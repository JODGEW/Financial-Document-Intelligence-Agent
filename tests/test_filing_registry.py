"""Tests for durable filing identity and the local filing registry.

Covers the identity model (filing_id vs document_family_id vs source_hash),
manifest validation, duplicate/conflict/failure outcomes, registry durability,
and construction of comparison.v1 FilingReferences from registry entries.
Everything here runs offline (no Bedrock, no Chroma writes).
"""

import json
import os
from datetime import date
from pathlib import Path

import pytest

import config
import filing_registry
import ingest
from filing_registry import (
    CONFLICT,
    DUPLICATE,
    FAILED,
    PARSED,
    ManifestError,
    filing_id_for,
    load_manifest,
    normalize_form_type,
    record_outcome,
    resolve_outcome,
    to_filing_reference,
)

PDF_2024 = "acme-corp-10k-excerpt-2024.pdf"
PDF_2025 = "acme-corp-10k-excerpt-2025.pdf"


def _write_manifest(tmp_path, body: str) -> str:
    path = tmp_path / "manifest.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _docs_with_filing(tmp_path, name="acme-corp-10k-2025.md", text=None):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(exist_ok=True)
    (docs_dir / name).write_text(
        text or "# FORM 10-K\n\nAnnual report narrative with enough content.\n",
        encoding="utf-8",
    )
    return str(docs_dir)


def _manifest_for(name, period_end="2025-12-31", filing_date="2026-02-19"):
    return {
        name: {
            "company_name": "Acme Corporation",
            "form_type": "10-k",
            "period_end": date.fromisoformat(period_end),
            "filing_date": date.fromisoformat(filing_date),
        }
    }


# --- Identity derivation -----------------------------------------------------


def test_filing_id_is_deterministic_and_period_distinct():
    """Same identity -> same id (test 1); different periods -> distinct (test 3)."""
    a = filing_id_for("acme corporation", "10-K", date(2024, 12, 31))
    b = filing_id_for("acme corporation", "10-k", date(2024, 12, 31))
    c = filing_id_for("acme corporation", "10-k", date(2025, 12, 31))
    assert a == b == "acme-corporation:10-k:2024-12-31"
    assert c == "acme-corporation:10-k:2025-12-31"
    assert a != c
    # The reporting period is kept, never stripped.
    assert "2024-12-31" in a and "2025-12-31" in c


def test_different_companies_get_distinct_family_and_filing_ids(tmp_path):
    """Test 5: family and filing identities separate by company."""
    acme = filing_id_for("acme corporation", "10-k", date(2025, 12, 31))
    globex = filing_id_for("globex corporation", "10-k", date(2025, 12, 31))
    assert acme != globex

    a = tmp_path / "acme-corp-10k-2025.pdf"
    g = tmp_path / "globex-corp-10k-2025.pdf"
    a.write_bytes(b"a")
    g.write_bytes(b"g")
    assert ingest._document_id(a.name) != ingest._document_id(g.name)


def test_normalize_form_type():
    assert normalize_form_type("10-K") == "10-k"
    assert normalize_form_type(" 10 Q ") == "10-q"


# --- Manifest validation -----------------------------------------------------


def test_manifest_absent_is_empty(tmp_path):
    assert load_manifest(tmp_path / "missing.yaml") == {}


def test_manifest_parses_and_normalizes(tmp_path):
    path = _write_manifest(
        tmp_path,
        "filings:\n"
        "  a.pdf:\n"
        "    company_name: Acme Corporation\n"
        "    form_type: 10-K\n"
        "    period_end: 2025-12-31\n"
        "    filing_date: 2026-02-19\n",
    )
    manifest = load_manifest(path)
    entry = manifest["a.pdf"]
    assert entry["form_type"] == "10-k"
    assert entry["period_end"] == date(2025, 12, 31)
    assert entry["filing_date"] == date(2026, 2, 19)


def test_manifest_missing_identity_fails_before_indexing(tmp_path):
    """Test 10: incomplete filing identity is a hard error, not a guess."""
    path = _write_manifest(
        tmp_path,
        "filings:\n"
        "  a.pdf:\n"
        "    company_name: Acme Corporation\n"
        "    form_type: 10-K\n",  # period_end missing
    )
    with pytest.raises(ManifestError, match="period_end"):
        load_manifest(path)


def test_manifest_rejects_non_date_and_unknown_keys(tmp_path):
    with pytest.raises(ManifestError, match="ISO date"):
        load_manifest(
            _write_manifest(
                tmp_path,
                "filings:\n"
                "  a.pdf:\n"
                "    company_name: Acme\n"
                "    form_type: 10-K\n"
                "    period_end: not-a-date\n",
            )
        )
    with pytest.raises(ManifestError, match="unknown keys"):
        load_manifest(
            _write_manifest(
                tmp_path,
                "filings:\n"
                "  a.pdf:\n"
                "    company_name: Acme\n"
                "    form_type: 10-K\n"
                "    period_end: 2025-12-31\n"
                "    filed: 2026-01-01\n",
            )
        )


def test_real_corpus_manifest_is_valid():
    """The checked-in manifest for the controlled dataset must load."""
    manifest = load_manifest(config.CORPUS_MANIFEST_PATH)
    assert set(manifest) == {PDF_2024, PDF_2025}
    assert manifest[PDF_2024]["period_end"] < manifest[PDF_2025]["period_end"]


# --- Outcome resolution and persistence --------------------------------------


def _parsed(reg, source_path, source_hash, filing_id=None, **extra):
    return record_outcome(
        reg,
        source_path=source_path,
        source_name=Path(source_path).name,
        source_hash=source_hash,
        parse_status=PARSED,
        filing_id=filing_id,
        **extra,
    )


def test_same_source_reingest_is_parsed_and_single_entry(tmp_path):
    """Test 1/11: same content+name refreshes one parsed entry."""
    reg = tmp_path / "registry.jsonl"
    _parsed(reg, "a.pdf", "hash-a", filing_id="acme:10-k:2025-12-31")
    assert resolve_outcome("a.pdf", "hash-a", "acme:10-k:2025-12-31", reg) == (
        PARSED,
        None,
    )
    _parsed(reg, "a.pdf", "hash-a", filing_id="acme:10-k:2025-12-31")
    entries = filing_registry.list_entries(reg)
    assert len(entries) == 1
    assert entries[0]["parse_status"] == PARSED


def test_same_content_different_name_is_duplicate(tmp_path):
    """Test 2: identical bytes under a new name are not indexed twice."""
    reg = tmp_path / "registry.jsonl"
    _parsed(reg, "a.pdf", "hash-a")
    status, related = resolve_outcome("copy-of-a.pdf", "hash-a", None, reg)
    assert status == DUPLICATE
    assert related["source_path"] == "a.pdf"


def test_same_identity_different_content_is_conflict(tmp_path):
    """Test 4: one filing_id claimed by two different sources is explicit."""
    reg = tmp_path / "registry.jsonl"
    _parsed(reg, "a.pdf", "hash-a", filing_id="acme:10-k:2025-12-31")
    status, related = resolve_outcome(
        "other.pdf", "hash-b", "acme:10-k:2025-12-31", reg
    )
    assert status == CONFLICT
    assert related["source_path"] == "a.pdf"

    # Same source updating its own content is a version update, not a conflict.
    assert resolve_outcome("a.pdf", "hash-a2", "acme:10-k:2025-12-31", reg) == (
        PARSED,
        None,
    )


def test_registry_survives_reopen(tmp_path):
    """Test 14: a fresh reader (new process/object) sees persisted entries."""
    reg = tmp_path / "registry.jsonl"
    _parsed(
        reg,
        "a.pdf",
        "hash-a",
        filing_id="acme:10-k:2025-12-31",
        company_key="acme corporation",
        period_end="2025-12-31",
    )
    # Simulate a new process: re-read purely from disk via a fresh path object.
    reread = filing_registry.get_filing("acme:10-k:2025-12-31", str(reg))
    assert reread is not None
    assert reread["source_hash"] == "hash-a"
    raw = [json.loads(line) for line in reg.read_text().splitlines()]
    assert raw[0]["filing_id"] == "acme:10-k:2025-12-31"


def test_atomic_write_failure_leaves_previous_registry_intact(tmp_path, monkeypatch):
    """Test 15: an interrupted write never corrupts the existing file."""
    reg = tmp_path / "registry.jsonl"
    _parsed(reg, "a.pdf", "hash-a")
    before = reg.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(filing_registry.os, "replace", boom)
    with pytest.raises(OSError, match="simulated crash"):
        _parsed(reg, "b.pdf", "hash-b")

    assert reg.read_text(encoding="utf-8") == before
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], "temp file must be cleaned up on failure"


def test_failed_outcome_requires_known_status(tmp_path):
    with pytest.raises(ValueError, match="unknown parse_status"):
        record_outcome(
            tmp_path / "registry.jsonl",
            source_path="a.pdf",
            source_name="a.pdf",
            source_hash=None,
            parse_status="exploded",
        )


def test_queries_filter_and_order_by_period_end(tmp_path):
    """Find by company/form; chronological candidates ordered by period_end."""
    reg = tmp_path / "registry.jsonl"
    _parsed(
        reg, "b.pdf", "h2025",
        filing_id="acme-corporation:10-k:2025-12-31",
        company_key="acme corporation", form_type="10-k", period_end="2025-12-31",
    )
    _parsed(
        reg, "a.pdf", "h2024",
        filing_id="acme-corporation:10-k:2024-12-31",
        company_key="acme corporation", form_type="10-k", period_end="2024-12-31",
    )
    _parsed(
        reg, "g.pdf", "hg",
        filing_id="globex-corporation:10-k:2025-12-31",
        company_key="globex corporation", form_type="10-k", period_end="2025-12-31",
    )
    record_outcome(  # failed entries never surface as filings
        reg, source_path="bad.pdf", source_name="bad.pdf", source_hash=None,
        parse_status=FAILED, error_code="ValueError",
    )

    acme = filing_registry.chronological_candidates(
        "acme corporation", "10-K", reg
    )
    assert [f["filing_id"] for f in acme] == [
        "acme-corporation:10-k:2024-12-31",
        "acme-corporation:10-k:2025-12-31",
    ]
    assert acme[0]["period_end"] < acme[1]["period_end"]
    assert len(filing_registry.list_filings(reg)) == 3
    assert filing_registry.get_filing("nope", reg) is None


# --- Ingestion integration ---------------------------------------------------


def test_successful_ingestion_persists_parsed_entry(tmp_path):
    """Test 11: load_documents records a parsed outcome with identity."""
    docs_dir = _docs_with_filing(tmp_path)
    reg = tmp_path / "registry.jsonl"
    docs = ingest.load_documents(
        docs_dir,
        manifest=_manifest_for("acme-corp-10k-2025.md"),
        registry_path=reg,
    )
    assert docs, "the filing should load"
    entry = filing_registry.get_filing("acme-corporation:10-k:2025-12-31", reg)
    assert entry is not None
    assert entry["parse_status"] == PARSED
    assert entry["identity_source"] == "manifest"
    assert entry["company_key"] == "acme corporation"
    assert entry["period_end"] == "2025-12-31"
    assert entry["loader"] == "loaders.markdown"
    assert entry["document_family_id"] == "acme-corp-10k"
    # Chunk metadata carries the filing identity and the explicit family key.
    meta = docs[0].metadata
    assert meta["filing_id"] == "acme-corporation:10-k:2025-12-31"
    assert meta["document_family_id"] == meta["document_id"] == "acme-corp-10k"
    assert meta["period_end"] == "2025-12-31"
    assert meta["filing_date"] == "2026-02-19"


def test_duplicate_content_not_indexed_twice(tmp_path):
    """Tests 2/12: a renamed byte-identical copy records duplicate, loads nothing."""
    docs_dir = Path(_docs_with_filing(tmp_path))
    (docs_dir / "copy-of-filing.md").write_bytes(
        (docs_dir / "acme-corp-10k-2025.md").read_bytes()
    )
    reg = tmp_path / "registry.jsonl"
    docs = ingest.load_documents(
        str(docs_dir),
        manifest=_manifest_for("acme-corp-10k-2025.md"),
        registry_path=reg,
    )
    loaded_sources = {d.metadata["source_name"] for d in docs}
    assert loaded_sources == {"acme-corp-10k-2025.md"}

    outcomes = {
        e["source_path"]: e for e in filing_registry.list_entries(reg)
    }
    dup = outcomes["copy-of-filing.md"]
    assert dup["parse_status"] == DUPLICATE
    assert dup["duplicate_of"] == "acme-corp-10k-2025.md"

    # Re-running is stable: still one parsed source, one duplicate record.
    docs_again = ingest.load_documents(
        str(docs_dir),
        manifest=_manifest_for("acme-corp-10k-2025.md"),
        registry_path=reg,
    )
    assert {d.metadata["source_name"] for d in docs_again} == {
        "acme-corp-10k-2025.md"
    }


def test_identity_conflict_not_indexed(tmp_path):
    """Test 4 end-to-end: two different files claiming one filing identity."""
    docs_dir = Path(_docs_with_filing(tmp_path))
    (docs_dir / "restated.md").write_text(
        "# FORM 10-K\n\nDifferent restated content entirely.\n", encoding="utf-8"
    )
    manifest = _manifest_for("acme-corp-10k-2025.md")
    manifest.update(_manifest_for("restated.md"))  # same identity on purpose
    reg = tmp_path / "registry.jsonl"

    docs = ingest.load_documents(str(docs_dir), manifest=manifest, registry_path=reg)
    loaded = {d.metadata["source_name"] for d in docs}
    assert loaded == {"acme-corp-10k-2025.md"}

    outcomes = {e["source_path"]: e for e in filing_registry.list_entries(reg)}
    conflict = outcomes["restated.md"]
    assert conflict["parse_status"] == CONFLICT
    assert conflict["conflict_with"] == "acme-corp-10k-2025.md"
    # The already-parsed holder is untouched.
    assert outcomes["acme-corp-10k-2025.md"]["parse_status"] == PARSED


def test_parse_failure_persists_safe_failed_outcome(tmp_path):
    """Test 13: a loader crash records failed with a safe code, no traceback."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "broken.pdf").write_bytes(b"not a real pdf")
    reg = tmp_path / "registry.jsonl"

    docs = ingest.load_documents(str(docs_dir), registry_path=reg)
    assert docs == []

    entries = filing_registry.list_entries(reg)
    assert len(entries) == 1
    failed = entries[0]
    assert failed["parse_status"] == FAILED
    assert failed["error_code"]
    assert failed["error_summary"]
    assert len(failed["error_summary"]) <= 200
    assert "Traceback" not in failed["error_summary"]
    assert failed["filing_id"] is None


def test_registry_opt_out_preserves_library_behavior(tmp_path):
    """Without registry_path/manifest, load_documents writes nothing anywhere."""
    docs_dir = _docs_with_filing(tmp_path)
    docs = ingest.load_documents(docs_dir)
    assert docs
    assert "filing_id" not in docs[0].metadata
    assert not (tmp_path / "registry.jsonl").exists()


# --- FilingReference construction (comparison.v1 producer) -------------------


def test_filing_reference_from_controlled_dataset(tmp_path):
    """Test 16: both controlled filings yield valid comparison.v1 references
    that pass the cross-filing invariants as a (previous, current) pair."""
    from governance.comparison_schema import (
        ComparisonResult,
        FilingReference,
        summarize_validation,
    )

    manifest = load_manifest(config.CORPUS_MANIFEST_PATH)
    reg = tmp_path / "registry.jsonl"
    ingest.load_documents(config.DOCS_DIR, manifest=manifest, registry_path=reg)

    filings = filing_registry.chronological_candidates(
        "acme corporation", "10-k", reg
    )
    assert [f["source_name"] for f in filings] == [PDF_2024, PDF_2025]

    previous, current = (to_filing_reference(f) for f in filings)
    assert isinstance(previous, FilingReference)
    assert previous.document_id != current.document_id
    assert previous.company_key == current.company_key
    assert previous.form_type == current.form_type == "10-k"
    assert previous.period_end < current.period_end
    assert previous.filing_date == date(2025, 2, 20)
    assert previous.version_hash and len(previous.version_hash) == 12

    result = ComparisonResult(
        comparison_id="cmp-controlled-dataset",
        previous_filing=previous,
        current_filing=current,
        section_scope=["item_1a_risk_factors"],
        changes=[],
        validation_summary=summarize_validation([]),
        created_at="2026-07-29T00:00:00Z",
        producer="test.v1",
    )
    assert result.previous_filing.period_end.isoformat() == "2024-12-31"


def test_filing_reference_refuses_unparsed_or_identityless_entries(tmp_path):
    reg = tmp_path / "registry.jsonl"
    entry = record_outcome(
        reg,
        source_path="a.pdf",
        source_name="a.pdf",
        source_hash="h",
        parse_status=FAILED,
        error_code="ValueError",
    )
    with pytest.raises(ValueError, match="parsed registry entry"):
        to_filing_reference(entry)

    generic = record_outcome(
        reg,
        source_path="note.txt",
        source_name="note.txt",
        source_hash="h2",
        parse_status=PARSED,
    )
    with pytest.raises(ValueError, match="filing"):
        to_filing_reference(generic)


# --- filing_date is never guessed --------------------------------------------


def test_filing_date_and_period_never_inferred_from_filename_or_mtime(tmp_path):
    """Test 9: without a manifest, no filing_date/period_end/filing_id exist,
    even when the filename carries a year and the file has an mtime."""
    source = tmp_path / "acme-corp-10k-excerpt-2025.pdf"
    source.write_bytes(b"Acme Corporation Form 10-K fiscal year 2025")
    os.utime(source, (1_500_000_000, 1_500_000_000))  # arbitrary mtime

    metadata = ingest.build_source_metadata(source, "Form 10-K fiscal year 2025")
    assert "filing_date" not in metadata
    assert "period_end" not in metadata
    assert "filing_id" not in metadata
    # year remains a filter hint, not a filing identity.
    assert metadata["year"] == 2025

    reg = tmp_path / "reg.jsonl"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "acme-corp-10k-2025.md").write_text("# FORM 10-K\ntext")
    ingest.load_documents(str(docs_dir), registry_path=reg)
    entry = filing_registry.list_entries(reg)[0]
    assert entry["filing_date"] is None
    assert entry["period_end"] is None
    assert entry["filing_id"] is None
    assert entry["identity_source"] == "inferred"


def test_duplicate_detection_is_order_independent(tmp_path):
    """A stray copy that SORTS BEFORE the manifest-listed original must still
    become the duplicate — the listed filing keeps canonical identity."""
    docs_dir = Path(_docs_with_filing(tmp_path))  # acme-corp-10k-2025.md
    # "aaa-copy.md" sorts before "acme-corp-10k-2025.md".
    (docs_dir / "aaa-copy.md").write_bytes(
        (docs_dir / "acme-corp-10k-2025.md").read_bytes()
    )
    reg = tmp_path / "registry.jsonl"
    docs = ingest.load_documents(
        str(docs_dir),
        manifest=_manifest_for("acme-corp-10k-2025.md"),
        registry_path=reg,
    )

    assert {d.metadata["source_name"] for d in docs} == {"acme-corp-10k-2025.md"}
    outcomes = {e["source_path"]: e for e in filing_registry.list_entries(reg)}
    assert outcomes["aaa-copy.md"]["parse_status"] == DUPLICATE
    assert outcomes["aaa-copy.md"]["duplicate_of"] == "acme-corp-10k-2025.md"
    parsed = outcomes["acme-corp-10k-2025.md"]
    assert parsed["parse_status"] == PARSED
    assert parsed["filing_id"] == "acme-corporation:10-k:2025-12-31"
