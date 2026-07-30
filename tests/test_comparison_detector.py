"""Tests for deterministic Item 1A change detection (comparison_detector.py).

Builds the controlled corpus once into a temporary Chroma index (fake
embeddings — metadata `get` never embeds) plus a temporary filing registry,
then exercises section loading, unit extraction, alignment, change semantics,
evidence resolution, validation output, persistence/lifecycle, the API
surface, and the controlled labeled evaluation. Entirely offline.
"""

import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.embeddings import Embeddings

import api
import comparison_detector
import comparison_store
import config
import filing_registry
import ingest
from comparison_detector import (
    DETECTOR_VERSION,
    DetectionInputsStale,
    DetectionInternalError,
    DetectionNotReady,
    SectionChunk,
    SectionLoad,
    align_units,
    detect,
    detect_changes,
    extract_units,
    load_section,
)
from governance.comparison_schema import load_comparison
from tests.auth_helpers import authorization_headers

PREV_ID = "acme-corporation:10-k:2024-12-31"
CURR_ID = "acme-corporation:10-k:2025-12-31"
SECTION_KEY = "item_1a_risk_factors"
LABELS_PATH = Path(__file__).parent / "fixtures" / "comparison_item1a_labels.json"

client = TestClient(api.app, headers=authorization_headers())


class _FakeEmbeddings(Embeddings):
    """Offline embeddings for seeding; raises if detection ever embeds."""

    def __init__(self):
        self.query_calls = 0

    def embed_documents(self, texts):
        return [[float(len(t) % 7), 1.0] for t in texts]

    def embed_query(self, text):
        self.query_calls += 1
        raise AssertionError("vector retrieval must not be used by the detector")


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    """Real controlled corpus ingested into tmp registry + tmp Chroma once."""
    from langchain_chroma import Chroma

    td = tmp_path_factory.mktemp("detector-corpus")
    registry = td / "registry.jsonl"
    manifest = filing_registry.load_manifest()
    docs = ingest.load_documents(
        config.DOCS_DIR, manifest=manifest, registry_path=registry
    )
    chunks = ingest.split_documents(docs)
    unique, ids = ingest._dedupe_by_id(chunks)
    counts = {}
    for chunk in unique:
        rel = chunk.metadata.get("source_path")
        counts[rel] = counts.get(rel, 0) + 1
    filing_registry.update_chunk_counts(counts, registry)

    embeddings = _FakeEmbeddings()
    chroma = Chroma(
        collection_name="detectoridx",
        persist_directory=str(td / "chroma"),
        embedding_function=embeddings,
    )
    chroma.add_documents(documents=unique, ids=ids)
    return SimpleNamespace(
        registry=registry, chroma=chroma, chunks=unique, embeddings=embeddings
    )


@pytest.fixture
def db(tmp_path):
    return tmp_path / "comparisons.db"


def _entry(corpus, filing_id):
    return filing_registry.get_filing(filing_id, corpus.registry)


def _detected(corpus, db, prev=PREV_ID, curr=CURR_ID):
    record, _ = comparison_store.create_comparison(
        prev, curr, db_path=db, registry_path=corpus.registry
    )
    result, created = detect(
        record["comparison_id"],
        db_path=db,
        registry_path=corpus.registry,
        chroma_client=corpus.chroma,
    )
    return record, result, created


def _changes_by_type(result):
    grouped = {}
    for change in result["changes"]:
        grouped.setdefault(change["change_type"], []).append(change)
    return grouped


# --- Section metadata and loading (tests 1-2) --------------------------------


def test_controlled_chunks_receive_section_key(corpus):
    """Test 1: Item 1A chunks of both filings carry the canonical key; body
    text and generic documents never do."""
    for filing_source, expected in (
        ("acme-corp-10k-excerpt-2024.pdf", 4),
        ("acme-corp-10k-excerpt-2025.pdf", 6),
    ):
        tagged = [
            c
            for c in corpus.chunks
            if c.metadata.get("source_name") == filing_source
            and c.metadata.get("section_key") == SECTION_KEY
        ]
        assert len(tagged) == expected
        assert all(
            "ITEM 1A" in (c.metadata.get("section_title") or "").upper()
            for c in tagged
        )
    # The research note MENTIONS risk factors in body text but has no Item 1A
    # heading — and generic documents remain unaffected.
    generic = [
        c
        for c in corpus.chunks
        if c.metadata.get("source_name")
        in ("compliance-policy-personal-trading.md", "cybersecurity-disclosure-research-note.txt")
    ]
    assert generic
    assert all("section_key" not in c.metadata for c in generic)
    # Every chunk got a deterministic sequence number.
    assert all("chunk_seq" in c.metadata for c in corpus.chunks)


def test_complete_sections_load_by_filing_and_section_key(corpus):
    """Test 2: full sections load by metadata, ordered, completeness proven."""
    for filing_id, expected_chunks in ((PREV_ID, 4), (CURR_ID, 6)):
        load = load_section(
            filing_id, SECTION_KEY, corpus.chroma, _entry(corpus, filing_id)
        )
        assert load.status == "loaded"
        assert load.complete is True
        assert len(load.chunks) == expected_chunks
        seqs = [chunk.chunk_seq for chunk in load.chunks]
        assert seqs == sorted(seqs)
        assert all(chunk.filing_id == filing_id for chunk in load.chunks)


def test_vector_retrieval_is_never_invoked(corpus, db, monkeypatch):
    """Test 3: similarity search paths poisoned; embed_query raises; detection
    still succeeds because it only uses metadata get."""

    def poisoned(*args, **kwargs):
        raise AssertionError("similarity search must not be called")

    monkeypatch.setattr(corpus.chroma, "similarity_search", poisoned, raising=False)
    monkeypatch.setattr(
        corpus.chroma, "similarity_search_with_score", poisoned, raising=False
    )
    monkeypatch.setattr(corpus.chroma, "as_retriever", poisoned, raising=False)

    _record, result, created = _detected(corpus, db)
    assert created is True
    assert len(result["changes"]) == 5
    assert corpus.embeddings.query_calls == 0


# --- Units and alignment (tests 4-5, 11) -------------------------------------


def test_risk_factor_units_are_deterministic(corpus):
    """Test 4: repeated extraction yields identical keys, hashes, evidence."""
    load = load_section(CURR_ID, SECTION_KEY, corpus.chroma, _entry(corpus, CURR_ID))
    first = extract_units(load.chunks, CURR_ID)
    second = extract_units(load.chunks, CURR_ID)
    assert [u.unit_key for u in first] == [u.unit_key for u in second]
    assert [u.content_hash for u in first] == [u.content_hash for u in second]
    assert [[c.chunk_id for c in u.chunks] for u in first] == [
        [c.chunk_id for c in u.chunks] for u in second
    ]
    assert [u.unit_key for u in first] == [
        "item-1a-preamble",
        "cybersecurity-and-data-security-risks",
        "regulatory-and-compliance-risks",
        "concentration-and-customer-risks",
        "artificial-intelligence-and-model-risk",
    ]


def test_reconstruction_matches_source_text(corpus):
    """Overlap dedup is exact: the normalized reconstruction is a substring of
    the filing's full normalized text (a section is contiguous in the doc)."""
    import fitz

    for filing_id, source in (
        (PREV_ID, "acme-corp-10k-excerpt-2024.pdf"),
        (CURR_ID, "acme-corp-10k-excerpt-2025.pdf"),
    ):
        load = load_section(filing_id, SECTION_KEY, corpus.chroma, _entry(corpus, filing_id))
        text, _spans = comparison_detector._reconstruct(load.chunks)
        with fitz.open(str(Path(config.DOCS_DIR) / source)) as pdf:
            full = " ".join(page.get_text() for page in pdf)
        normalized_full = " ".join(full.split())
        normalized_section = " ".join(text.split())
        assert normalized_section in normalized_full


def test_unchanged_units_are_not_emitted(corpus, db):
    """Test 5: the byte-identical preamble aligns and produces no change."""
    _record, result, _ = _detected(corpus, db)
    assert len(result["changes"]) == 5
    summaries = " ".join(change["summary"] for change in result["changes"])
    assert "introductory" not in summaries
    assert all(
        "item-1a-preamble" != change.get("undetermined_reason") for change in result["changes"]
    )


def test_ambiguous_alignment_produces_undetermined():
    """Test 11: duplicate heading keys on one side -> undetermined, never a
    guessed pairing."""

    def unit_chunks(filing_id, chunk_id, text):
        return [
            SectionChunk(
                chunk_id=chunk_id,
                text=text,
                filing_id=filing_id,
                source_name="x.pdf",
                chunk_seq=0,
                page=1,
                section_title="ITEM 1A. RISK FACTORS",
            )
        ]

    prev_load = SectionLoad(
        filing_id="prev-f",
        status="loaded",
        complete=True,
        chunks=unit_chunks(
            "prev-f",
            "x.pdf:1:aaa",
            "Cyber Risks\nOld body one.\nCyber Risks\nOld body two.",
        ),
    )
    curr_load = SectionLoad(
        filing_id="curr-f",
        status="loaded",
        complete=True,
        chunks=unit_chunks("curr-f", "y.pdf:1:bbb", "Cyber Risks\nNew body."),
    )
    changes = detect_changes(prev_load, curr_load, "prev-f", "curr-f")
    assert [c["change_type"] for c in changes] == ["undetermined"]
    assert changes[0]["undetermined_reason"].startswith("ambiguous_unit_alignment")


# --- Change semantics on the controlled pair (tests 6-8) ---------------------


def test_known_modified_risk_factor(corpus, db):
    """Test 6: cybersecurity changed -> modified with two-sided evidence."""
    _record, result, _ = _detected(corpus, db)
    modified = {
        c["summary"]: c for c in _changes_by_type(result)["modified"]
    }
    cyber = next(
        c
        for summary, c in modified.items()
        if "Cybersecurity and Data Security Risks" in summary
    )
    assert cyber["previous_evidence"] and cyber["current_evidence"]
    assert all(ref["document_id"] == PREV_ID for ref in cyber["previous_evidence"])
    assert all(ref["document_id"] == CURR_ID for ref in cyber["current_evidence"])
    assert all(
        ref["section_key"] == SECTION_KEY
        for ref in cyber["previous_evidence"] + cyber["current_evidence"]
    )


def test_known_added_risk_factor(corpus, db):
    """Test 7: the FY2025-only AI risk factor emits added, current side only."""
    _record, result, _ = _detected(corpus, db)
    added = _changes_by_type(result)["added"]
    assert len(added) == 1
    assert "Artificial Intelligence and Model Risk" in added[0]["summary"]
    assert added[0]["previous_evidence"] == []
    assert all(ref["document_id"] == CURR_ID for ref in added[0]["current_evidence"])


def test_known_removed_risk_factor(corpus, db):
    """Test 8: the FY2024-only LIBOR risk factor emits removed."""
    _record, result, _ = _detected(corpus, db)
    removed = _changes_by_type(result)["removed"]
    assert len(removed) == 1
    assert "Reference Rate Transition Risks" in removed[0]["summary"]
    assert removed[0]["current_evidence"] == []
    assert all(
        ref["document_id"] == PREV_ID for ref in removed[0]["previous_evidence"]
    )


# --- Missing/incomplete sections (tests 9-10) --------------------------------


def _register_unindexed(corpus, filing_id, period_end):
    """A filing that exists as parsed in the registry but has no indexed
    chunks (e.g. ingested elsewhere, index never rebuilt)."""
    filing_registry.record_outcome(
        corpus.registry,
        source_path=f"acme-{period_end[:4]}.pdf",
        source_name=f"acme-{period_end[:4]}.pdf",
        source_hash=f"hash-{period_end[:4]}",
        parse_status=filing_registry.PARSED,
        filing_id=filing_id,
        company_key="acme corporation",
        company_name="Acme Corporation",
        form_type="10-k",
        period_end=period_end,
        document_family_id="acme-corp-10k-excerpt",
        identity_source="manifest",
        chunk_count=5,
    )


def test_missing_previous_section_never_mass_added(corpus, db):
    """Test 9: unindexed previous filing -> one undetermined change with the
    previous_section_missing reason; nothing is claimed added."""
    unindexed = "acme-corporation:10-k:2023-12-31"
    _register_unindexed(corpus, unindexed, "2023-12-31")
    _record, result, _ = _detected(corpus, db, prev=unindexed, curr=PREV_ID)
    assert [c["change_type"] for c in result["changes"]] == ["undetermined"]
    change = result["changes"][0]
    assert change["undetermined_reason"].startswith("previous_section_missing")
    assert "could not be compared" in change["summary"]
    assert _changes_by_type(result).get("added") is None


def test_missing_current_section_never_mass_removed(corpus, db):
    """Test 10: unindexed current filing -> undetermined, nothing removed."""
    unindexed = "acme-corporation:10-k:2030-12-31"
    _register_unindexed(corpus, unindexed, "2030-12-31")
    _record, result, _ = _detected(corpus, db, prev=CURR_ID, curr=unindexed)
    assert [c["change_type"] for c in result["changes"]] == ["undetermined"]
    assert result["changes"][0]["undetermined_reason"].startswith(
        "current_section_missing"
    )
    assert _changes_by_type(result).get("removed") is None


def test_unprovable_completeness_downgrades_added_removed():
    """Unmatched units become undetermined when completeness can't be proven."""
    prev = SectionLoad(
        filing_id="prev-f",
        status="loaded",
        complete=False,
        incomplete_reason="indexed chunk count does not match registry",
        chunks=[
            SectionChunk("a.pdf:1:aaa", "Old Risks\nOld body.", "prev-f", "a.pdf", 0, 1, "ITEM 1A")
        ],
    )
    curr = SectionLoad(
        filing_id="curr-f",
        status="loaded",
        complete=True,
        chunks=[
            SectionChunk("b.pdf:1:bbb", "New Risks\nNew body.", "curr-f", "b.pdf", 0, 1, "ITEM 1A")
        ],
    )
    changes = detect_changes(prev, curr, "prev-f", "curr-f")
    assert {c["change_type"] for c in changes} == {"undetermined"}
    assert all(
        c["undetermined_reason"].startswith("section_metadata_incomplete")
        for c in changes
    )


# --- Evidence resolution and schema guardrails (tests 12-13) -----------------


def test_every_evidence_reference_resolves_to_indexed_chunk(corpus, db):
    """Test 12: every ref's chunk_id fetches the exact indexed chunk, the
    excerpt comes from that chunk's text, and the filing matches."""
    _record, result, _ = _detected(corpus, db)
    refs = [
        ref
        for change in result["changes"]
        for ref in change["previous_evidence"] + change["current_evidence"]
    ]
    assert refs
    for ref in refs:
        got = corpus.chroma.get(ids=[ref["chunk_id"]], include=["documents", "metadatas"])
        assert got["ids"] == [ref["chunk_id"]], f"unresolvable: {ref['chunk_id']}"
        chunk_text = got["documents"][0]
        normalized = " ".join(chunk_text.split())
        assert ref["excerpt"] == normalized[:700].strip()
        assert got["metadatas"][0]["filing_id"] == ref["document_id"]
        assert got["metadatas"][0]["section_key"] == SECTION_KEY


def test_wrong_side_evidence_is_rejected_by_schema(corpus, db):
    """Test 13: comparison.v1 refuses detector output with swapped sides."""
    _record, result, _ = _detected(corpus, db)
    tampered = json.loads(json.dumps(result))
    modified = next(
        c for c in tampered["changes"] if c["change_type"] == "modified"
    )
    modified["previous_evidence"][0]["document_id"] = CURR_ID
    with pytest.raises(Exception, match="previous_filing.document_id"):
        load_comparison(tampered)


# --- Validation output (tests 14-18) -----------------------------------------


def test_implemented_checks_populated_accurately(corpus, db):
    """Test 14: evidence_presence / entity / period are computed, versioned."""
    _record, result, _ = _detected(corpus, db)
    for change in result["changes"]:
        checks = {c["check"]: c for c in change["validation"]}
        assert checks["evidence_presence"]["status"] == "passed"
        assert checks["evidence_presence"]["validator_version"] == "evidence_presence.v1"
        assert checks["entity_consistency"]["status"] == "passed"
        assert checks["period_consistency"]["status"] == "passed"


def test_all_six_checks_are_implemented_or_not_applicable(corpus, db):
    """No broad not_run remains: the three content validators now compute a
    real status with a version and reason code on every change."""
    _record, result, _ = _detected(corpus, db)
    for change in result["changes"]:
        checks = {c["check"]: c for c in change["validation"]}
        assert len(checks) == 6
        assert all(c["status"] != "not_run" for c in checks.values())

        assert checks["citation_support"]["status"] == "passed"
        assert checks["citation_support"]["reason_code"] == "citation_summary_supported"
        assert checks["citation_support"]["validator_version"] == "citation_support.v1"

        # Deterministic summaries carry no numeric claims -> not_applicable,
        # never a hollow "passed".
        assert checks["numeric_consistency"]["status"] == "not_applicable"
        assert checks["numeric_consistency"]["reason_code"] == "no_numeric_claim"
        assert (
            checks["numeric_consistency"]["validator_version"]
            == "numeric_consistency.v1"
        )

        direction = checks["direction_consistency"]
        assert direction["validator_version"] == "direction_consistency.v1"
        if change["change_type"] in ("added", "removed"):
            assert direction["status"] == "passed"
            assert direction["reason_code"] == "direction_supported"
        else:  # modified summaries make no increase/decrease/unchanged claim
            assert direction["status"] == "not_applicable"
            assert direction["reason_code"] == "no_directional_claim"


def test_validation_summary_risk_and_review(corpus, db):
    """Tests 16-18: summary tallies match; risk/review are untouched
    placeholders."""
    _record, result, _ = _detected(corpus, db)
    summary = result["validation_summary"]
    # 5 changes x 6 checks: base three passed (15) + citation passed (5) +
    # numeric not_applicable (5) + direction passed on added/removed (2) and
    # not_applicable on the three modified changes.
    assert summary == {
        "total_checks": 30,
        "passed": 22,
        "failed": 0,
        "not_run": 0,
        "not_applicable": 8,
    }
    assert result["risk"] == {
        "decision": "not_evaluated",
        "reason_codes": [],
        "risk_score": None,
        "risk_level": None,
    }
    assert result["review"] == {"status": "not_required", "review_id": None}


# --- Persistence, lifecycle, idempotency (tests 19-24) -----------------------


def test_detection_persists_result_and_transitions(corpus, db):
    """Test 19: comparisons -> detected; comparison_results holds the doc."""
    record, result, created = _detected(corpus, db)
    assert created is True
    row = comparison_store.get_comparison(record["comparison_id"], db_path=db)
    assert row["status"] == "detected"
    stored = comparison_store.get_result(record["comparison_id"], db_path=db)
    assert stored["detector_version"] == DETECTOR_VERSION
    assert stored["previous_source_hash"] == _entry(corpus, PREV_ID)["source_hash"]
    assert stored["result"] == result


def test_reopen_returns_identical_result(corpus, db):
    """Test 20: a fresh read returns the identical wire document, and it
    still validates against comparison.v1."""
    record, result, _ = _detected(corpus, db)
    stored = comparison_store.get_result(record["comparison_id"], db_path=db)
    assert stored["result"] == result
    load_comparison(stored["result"])  # revalidates cleanly


def test_repeat_detection_is_idempotent(corpus, db):
    """Test 21: same inputs + detector version -> stored result, created=False."""
    record, result, _ = _detected(corpus, db)
    again, created = detect(
        record["comparison_id"],
        db_path=db,
        registry_path=corpus.registry,
        chroma_client=corpus.chroma,
    )
    assert created is False
    assert again == result


def test_determinism_across_fresh_runs(corpus, tmp_path):
    """Two fresh databases produce identical results minus created_at, with
    identical result hashes."""
    results, hashes = [], []
    for name in ("one.db", "two.db"):
        db = tmp_path / name
        record, result, _ = _detected(corpus, db)
        results.append({k: v for k, v in result.items() if k != "created_at"})
        hashes.append(
            comparison_store.get_result(record["comparison_id"], db_path=db)[
                "result_hash"
            ]
        )
    assert results[0] == results[1]
    assert hashes[0] == hashes[1]


def test_concurrent_detection_produces_one_result(corpus, tmp_path):
    """Test 22: racing detections -> one stored row, one created=True.

    Since durable attempts landed, a racing request no longer re-runs the
    detector: it either loses the start race and gets DetectionInProgress, or
    arrives after the winner committed and gets the idempotent stored result.
    Either way exactly one execution happens and exactly one result exists.
    """
    db = tmp_path / "concurrent.db"
    record, _ = comparison_store.create_comparison(
        PREV_ID, CURR_ID, db_path=db, registry_path=corpus.registry
    )

    def attempt(_):
        try:
            return detect(
                record["comparison_id"],
                db_path=db,
                registry_path=corpus.registry,
                chroma_client=corpus.chroma,
            )
        except comparison_detector.DetectionInProgress as exc:
            return exc

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(attempt, range(6)))

    succeeded = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    in_progress = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, comparison_detector.DetectionInProgress)
    ]
    assert len(succeeded) + len(in_progress) == 6
    assert sum(1 for _r, created in succeeded if created) == 1
    assert all(exc.code == "detection_in_progress" for exc in in_progress)
    created_result = next(result for result, created in succeeded if created)
    assert all(result == created_result for result, _ in succeeded)
    with closing(sqlite3.connect(db)) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM comparison_results").fetchone()[0]
            == 1
        )
        # Exactly one execution ran: one attempt, and it succeeded.
        attempts = conn.execute(
            "SELECT status, attempt_number FROM comparison_detection_attempts"
        ).fetchall()
        assert attempts == [("succeeded", 1)]


def test_stale_source_hash_is_rejected(corpus, db):
    """Test 23: changed registry source hash -> 409-shaped stale error, and
    the stored result is not silently re-presented or overwritten."""
    record, result, _ = _detected(corpus, db)
    entry = _entry(corpus, PREV_ID)
    original_hash = entry["source_hash"]
    try:
        mutated = dict(entry)
        mutated["source_hash"] = "0" * 64
        filing_registry.record_outcome(
            corpus.registry,
            **{
                k: mutated[k]
                for k in (
                    "source_path", "source_name", "source_hash", "parse_status",
                    "document_family_id", "filing_id", "company_key",
                    "company_name", "form_type", "period_end", "filing_date",
                    "identity_source", "loader", "chunk_count",
                )
            },
        )
        with pytest.raises(DetectionInputsStale):
            detect(
                record["comparison_id"],
                db_path=db,
                registry_path=corpus.registry,
                chroma_client=corpus.chroma,
            )
        stored = comparison_store.get_result(record["comparison_id"], db_path=db)
        assert stored["result"] == result  # untouched
    finally:
        restored = dict(entry)
        restored["source_hash"] = original_hash
        filing_registry.record_outcome(
            corpus.registry,
            **{
                k: restored[k]
                for k in (
                    "source_path", "source_name", "source_hash", "parse_status",
                    "document_family_id", "filing_id", "company_key",
                    "company_name", "form_type", "period_end", "filing_date",
                    "identity_source", "loader", "chunk_count",
                )
            },
        )


def test_detector_failure_transitions_to_failed(corpus, db, monkeypatch):
    """Test 24: an unexpected fault marks the comparison failed with a stable
    code and a safe summary, and later detects are lifecycle-blocked."""
    record, _ = comparison_store.create_comparison(
        PREV_ID, CURR_ID, db_path=db, registry_path=corpus.registry
    )

    def boom(*args, **kwargs):
        raise RuntimeError("kaboom /absolute/secret/path SELECT * FROM comparisons")

    monkeypatch.setattr(comparison_detector, "detect_changes", boom)
    with pytest.raises(DetectionInternalError) as excinfo:
        detect(
            record["comparison_id"],
            db_path=db,
            registry_path=corpus.registry,
            chroma_client=corpus.chroma,
        )
    assert "/absolute" not in str(excinfo.value)
    assert "SELECT" not in str(excinfo.value)

    row = comparison_store.get_comparison(record["comparison_id"], db_path=db)
    assert row["status"] == "failed"
    assert row["failure_code"] == "detector_internal_error"
    assert "/absolute" not in row["failure_summary"]
    assert "SELECT" not in row["failure_summary"]

    monkeypatch.undo()
    with pytest.raises(DetectionNotReady):
        detect(
            record["comparison_id"],
            db_path=db,
            registry_path=corpus.registry,
            chroma_client=corpus.chroma,
        )


def test_old_version_result_superseded_not_stale_and_still_readable(corpus, db):
    """A result stored by an older detector version: GET keeps serving it,
    re-detection raises the SUPERSEDED code (not comparison_inputs_stale —
    source hashes did not change), and nothing is overwritten."""
    record, result, _ = _detected(corpus, db)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "UPDATE comparison_results SET detector_version = 'item1a_detector.v1' "
            "WHERE comparison_id = ?",
            (record["comparison_id"],),
        )

    stored = comparison_store.get_result(record["comparison_id"], db_path=db)
    assert stored["result"] == result  # old-version output remains readable

    with pytest.raises(
        comparison_detector.DetectionVersionSuperseded
    ) as excinfo:
        detect(
            record["comparison_id"],
            db_path=db,
            registry_path=corpus.registry,
            chroma_client=corpus.chroma,
        )
    assert excinfo.value.code == "detector_version_superseded"
    assert "item1a_detector.v1" in str(excinfo.value)

    after = comparison_store.get_result(record["comparison_id"], db_path=db)
    assert after["detector_version"] == "item1a_detector.v1"  # untouched
    assert after["result"] == result


# --- API surface (tests 25-27) -----------------------------------------------


@pytest.fixture
def api_env(corpus, tmp_path, monkeypatch):
    """Route the app at the controlled corpus + a tmp comparison db."""
    db = tmp_path / "api.db"
    monkeypatch.setattr(config, "FILING_REGISTRY_PATH", str(corpus.registry))
    monkeypatch.setattr(config, "COMPARISON_DB_PATH", str(db))
    monkeypatch.setattr(comparison_detector, "open_index", lambda: corpus.chroma)
    return db


def _api_create(prev=PREV_ID, curr=CURR_ID):
    response = client.post(
        "/api/comparisons",
        json={"previousFilingId": prev, "currentFilingId": curr},
    )
    assert response.status_code in (200, 201)
    return response.json()["comparison"]["comparisonId"]


def test_detect_api_lifecycle_and_status_codes(api_env):
    """Test 26: 404 unknown; 201 new; 200 idempotent repeat; 409 stale."""
    assert client.post("/api/comparisons/cmp_nope/detect").status_code == 404
    assert client.get("/api/comparisons/cmp_nope/result").status_code == 404

    comparison_id = _api_create()
    missing = client.get(f"/api/comparisons/{comparison_id}/result")
    assert missing.status_code == 404  # created but not yet detected

    first = client.post(f"/api/comparisons/{comparison_id}/detect")
    assert first.status_code == 201
    assert first.json()["created"] is True

    second = client.post(f"/api/comparisons/{comparison_id}/detect")
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["result"] == first.json()["result"]

    # The entity routes serve the transitioned lifecycle (regression: the DTO
    # Literal must accept 'detected').
    record = client.get(f"/api/comparisons/{comparison_id}")
    assert record.status_code == 200
    assert record.json()["status"] == "detected"
    listed = client.get("/api/comparisons", params={"status": "detected"}).json()
    assert comparison_id in [item["comparisonId"] for item in listed]


def test_result_api_returns_comparison_v1_wire_shape(api_env):
    """Test 27: GET result is the persisted, schema-valid wire document."""
    comparison_id = _api_create()
    posted = client.post(f"/api/comparisons/{comparison_id}/detect").json()["result"]

    response = client.get(f"/api/comparisons/{comparison_id}/result")
    assert response.status_code == 200
    wire = response.json()
    assert wire == posted
    model = load_comparison(wire)  # validates as comparison.v1
    assert model.producer == DETECTOR_VERSION
    assert wire["schema_version"] == "comparison.v1"
    # No storage or filesystem detail in the wire document.
    text = response.text
    assert "/Users/" not in text and "sqlite" not in text.lower()
    assert str(api_env) not in text


def test_api_internal_failure_is_sanitized(api_env, monkeypatch, caplog):
    """Test 25: unexpected exceptions leak no paths, SQL, or content."""
    comparison_id = _api_create()
    secret = "kaboom /secret/corpus/path SELECT result_json FROM comparison_results"

    def boom(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(comparison_detector, "detect_changes", boom)
    with caplog.at_level(logging.ERROR, logger="api"):
        response = client.post(f"/api/comparisons/{comparison_id}/detect")

    assert response.status_code == 500
    assert "/secret" not in response.text
    assert "SELECT" not in response.text
    assert "RuntimeError" not in response.text
    detail = response.json()["detail"]
    assert detail["code"] == "detector_internal_error"
    assert detail["error_id"].startswith("err_")
    assert secret in caplog.text  # full fault preserved server-side

    # And the lifecycle honestly reflects the failure.
    record = client.get(f"/api/comparisons/{comparison_id}").json()
    assert record["status"] == "failed"
    assert record["failureCode"] == "detector_internal_error"


# --- Controlled labeled evaluation (test 28) ---------------------------------


def test_labeled_controlled_evaluation_metrics(corpus, db, capsys):
    """Test 28: deterministic metrics against the checked-in labels.

    Matching: exact (unit_key, change_type). Undetermined/failed outputs stay
    in the detected set (they would lower precision) — nothing is excluded.
    This is a controlled detector evaluation over synthetic fixtures, not a
    production benchmark.
    """
    labels = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    assert labels["previous_filing_id"] == PREV_ID
    assert labels["current_filing_id"] == CURR_ID

    _record, result, _ = _detected(corpus, db)

    def key_of(change):
        # change_id is chg-<sha1(change_type:unit_key)>; recover unit key by
        # matching against expected + detected headings via summary slugs is
        # fragile — instead recompute from the deterministic change_id.
        return change["change_id"]

    expected = {
        (item["unit_key"], item["change_type"])
        for item in labels["expected_changes"]
    }
    detected = set()
    for change in result["changes"]:
        for unit_key, change_type in [
            (item["unit_key"], change["change_type"])
            for item in labels["expected_changes"]
            + labels["expected_unchanged_units"]
        ]:
            if change["change_id"] == comparison_detector._change_id(
                change["change_type"], unit_key
            ):
                detected.add((unit_key, change["change_type"]))
                break
        else:
            detected.add((f"unlabeled:{change['change_id']}", change["change_type"]))

    true_positives = detected & expected
    precision = len(true_positives) / len(detected) if detected else 0.0
    recall = len(true_positives) / len(expected) if expected else 0.0

    # Type accuracy over expected units detected as ANY change type.
    detected_units = {unit for unit, _type in detected}
    matched_expected = [
        (unit, change_type)
        for unit, change_type in expected
        if unit in detected_units
    ]
    type_correct = [
        (unit, change_type)
        for unit, change_type in matched_expected
        if (unit, change_type) in detected
    ]
    type_accuracy = (
        len(type_correct) / len(matched_expected) if matched_expected else 0.0
    )

    refs = [
        ref
        for change in result["changes"]
        for ref in change["previous_evidence"] + change["current_evidence"]
    ]
    resolved = sum(
        1
        for ref in refs
        if corpus.chroma.get(ids=[ref["chunk_id"]])["ids"] == [ref["chunk_id"]]
    )
    evidence_resolution_rate = resolved / len(refs) if refs else 0.0

    unchanged_ok = all(
        not any(
            change["change_id"]
            == comparison_detector._change_id(change["change_type"], item["unit_key"])
            for change in result["changes"]
        )
        for item in labels["expected_unchanged_units"]
    )

    print(
        "\nControlled Item 1A evaluation (synthetic fixtures, not a benchmark):"
        f"\n  detected changes:          {len(detected)}"
        f"\n  expected changes:          {len(expected)}"
        f"\n  change precision:          {precision:.2f}"
        f"\n  change recall:             {recall:.2f}"
        f"\n  change-type accuracy:      {type_accuracy:.2f}"
        f"\n  evidence refs resolved:    {resolved}/{len(refs)}"
        f"\n  evidence-resolution rate:  {evidence_resolution_rate:.2f}"
        f"\n  unchanged units silent:    {unchanged_ok}"
    )

    assert precision == 1.0
    assert recall == 1.0
    assert type_accuracy == 1.0
    assert evidence_resolution_rate == 1.0
    assert unchanged_ok is True
