"""Tests for the persistent comparison entity store (comparison_store.py).

Covers pair validation against the filing registry, deterministic ids and
idempotent creation, concurrent identical creates, restart/reopen behavior,
and the pre-detection honesty boundary (no fake ComparisonResult). Offline.
"""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

import comparison_store
import filing_registry
from comparison_store import (
    STATUS_READY_FOR_DETECTION,
    ComparisonPairError,
    comparison_id_for,
    create_comparison,
    get_comparison,
    init_db,
    list_comparisons,
    normalize_section_scope,
)
from governance.comparison_schema import SECTION_ITEM_1A

PREV_ID = "acme-corporation:10-k:2024-12-31"
CURR_ID = "acme-corporation:10-k:2025-12-31"


def _register(reg, source_path, filing_id, *, period_end, source_hash,
              company_key="acme corporation", company_name="Acme Corporation",
              form_type="10-k", parse_status=filing_registry.PARSED, **extra):
    return filing_registry.record_outcome(
        reg,
        source_path=source_path,
        source_name=source_path,
        source_hash=source_hash,
        parse_status=parse_status,
        filing_id=filing_id,
        company_key=company_key,
        company_name=company_name,
        form_type=form_type,
        period_end=period_end,
        filing_date=None,
        document_family_id="acme-corp-10k-excerpt",
        identity_source="manifest",
        **extra,
    )


@pytest.fixture
def reg(tmp_path):
    """A registry with the two eligible Acme filings."""
    path = tmp_path / "registry.jsonl"
    _register(path, "acme-2024.pdf", PREV_ID, period_end="2024-12-31", source_hash="h24")
    _register(path, "acme-2025.pdf", CURR_ID, period_end="2025-12-31", source_hash="h25")
    return path


@pytest.fixture
def db(tmp_path):
    return tmp_path / "comparisons.db"


def _create(reg, db, prev=PREV_ID, curr=CURR_ID, scope=None):
    return create_comparison(prev, curr, scope, db_path=db, registry_path=reg)


# --- Creation, idempotency, durability ---------------------------------------


def test_valid_pair_creates_one_persistent_comparison(reg, db):
    """Test 1: eligible pair persists exactly one ready_for_detection record."""
    record, created = _create(reg, db)
    assert created is True
    assert record["status"] == STATUS_READY_FOR_DETECTION
    assert record["previous_filing_id"] == PREV_ID
    assert record["current_filing_id"] == CURR_ID
    assert record["section_scope"] == [SECTION_ITEM_1A]
    assert record["schema_version"] == "comparison.v1"
    assert record["workflow_version"] == comparison_store.WORKFLOW_VERSION
    assert record["created_at"].endswith("+00:00") or record["created_at"].endswith("Z")
    assert record["comparison_id"] == comparison_id_for(
        PREV_ID, CURR_ID, [SECTION_ITEM_1A]
    )
    assert record["failure_code"] is None

    with closing(sqlite3.connect(db)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM comparisons").fetchone()[0]
    assert count == 1


def test_record_is_pre_detection_not_a_comparison_result(reg, db):
    """The persisted entity must not masquerade as a completed ComparisonResult:
    no changes/evidence/validation/risk/review fields exist at all."""
    record, _ = _create(reg, db)
    forbidden = {"changes", "validation_summary", "risk", "review", "producer"}
    assert forbidden.isdisjoint(record)


def test_reopen_returns_same_record(reg, db):
    """Test 2: a fresh read on the same path (new connection) sees the row."""
    record, _ = _create(reg, db)
    reread = get_comparison(record["comparison_id"], db_path=db)
    assert reread == record


def test_identical_create_is_idempotent(reg, db):
    """Test 3: same logical comparison returns the same id, created=False."""
    first, created_first = _create(reg, db)
    second, created_second = _create(reg, db)
    assert created_first is True
    assert created_second is False
    assert second == first
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM comparisons").fetchone()[0] == 1


def test_concurrent_identical_creates_produce_one_row(reg, db):
    """Test 4: N racing creates -> one row, one created=True, same id."""
    init_db(db)

    def attempt(_):
        return _create(reg, db)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))

    ids = {record["comparison_id"] for record, _ in results}
    assert len(ids) == 1
    assert sum(1 for _, created in results if created) == 1
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM comparisons").fetchone()[0] == 1


def test_scope_normalization_dedupes_and_orders(reg, db):
    """Test 16: duplicate scope keys normalize deterministically to one key,
    and the normalized scope maps to the same logical comparison."""
    assert normalize_section_scope(
        [SECTION_ITEM_1A, f"  {SECTION_ITEM_1A}  ", SECTION_ITEM_1A]
    ) == [SECTION_ITEM_1A]
    record, created = _create(reg, db, scope=[SECTION_ITEM_1A, SECTION_ITEM_1A])
    assert record["section_scope"] == [SECTION_ITEM_1A]
    again, created_again = _create(reg, db, scope=[SECTION_ITEM_1A])
    assert again["comparison_id"] == record["comparison_id"]
    assert (created, created_again) == (True, False)


def test_init_db_is_idempotent(db):
    """Test 22: repeated initialization neither fails nor duplicates schema."""
    for _ in range(3):
        init_db(db)
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "comparisons" in tables


# --- Pair rejection (nothing persisted) --------------------------------------


def _reasons(excinfo):
    return excinfo.value.reasons


def _assert_nothing_persisted(db):
    if not db.exists():
        return
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM comparisons").fetchone()[0] == 0


def test_reversed_pair_rejected(reg, db):
    """Test 5: current-before-previous violates period ordering."""
    with pytest.raises(ComparisonPairError) as excinfo:
        _create(reg, db, prev=CURR_ID, curr=PREV_ID)
    assert "period_order_invalid" in _reasons(excinfo)
    _assert_nothing_persisted(db)


def test_same_filing_both_sides_rejected(reg, db):
    """Test 6."""
    with pytest.raises(ComparisonPairError) as excinfo:
        _create(reg, db, prev=PREV_ID, curr=PREV_ID)
    assert "identical_filings" in _reasons(excinfo)
    _assert_nothing_persisted(db)


def test_different_company_rejected(tmp_path, db):
    """Test 7."""
    reg = tmp_path / "registry.jsonl"
    _register(reg, "acme-2024.pdf", PREV_ID, period_end="2024-12-31", source_hash="h24")
    _register(
        reg, "globex-2025.pdf", "globex-corporation:10-k:2025-12-31",
        period_end="2025-12-31", source_hash="hg",
        company_key="globex corporation", company_name="Globex Corporation",
    )
    with pytest.raises(ComparisonPairError) as excinfo:
        _create(reg, db, curr="globex-corporation:10-k:2025-12-31")
    assert "company_mismatch" in _reasons(excinfo)
    _assert_nothing_persisted(db)


def test_different_form_rejected(tmp_path, db):
    """Test 8."""
    reg = tmp_path / "registry.jsonl"
    _register(reg, "acme-2024.pdf", PREV_ID, period_end="2024-12-31", source_hash="h24")
    _register(
        reg, "acme-10q.pdf", "acme-corporation:10-q:2025-09-30",
        period_end="2025-09-30", source_hash="hq", form_type="10-q",
    )
    with pytest.raises(ComparisonPairError) as excinfo:
        _create(reg, db, curr="acme-corporation:10-q:2025-09-30")
    assert "form_type_mismatch" in _reasons(excinfo)
    _assert_nothing_persisted(db)


def test_unknown_filings_rejected_per_side(reg, db):
    """Tests 9/10: unknown ids report the side they belong to."""
    with pytest.raises(ComparisonPairError) as excinfo:
        _create(reg, db, prev="acme-corporation:10-k:2019-12-31")
    assert _reasons(excinfo) == ["unknown_previous_filing"]

    with pytest.raises(ComparisonPairError) as excinfo:
        _create(reg, db, curr="acme-corporation:10-k:2030-12-31")
    assert _reasons(excinfo) == ["unknown_current_filing"]

    # Both sides unknown -> both codes at once.
    with pytest.raises(ComparisonPairError) as excinfo:
        _create(reg, db, prev="nope:10-k:2019-12-31", curr="nope:10-k:2020-12-31")
    assert _reasons(excinfo) == ["unknown_previous_filing", "unknown_current_filing"]
    _assert_nothing_persisted(db)


def test_duplicate_registry_entry_rejected(tmp_path, db):
    """Test 11: a filing_id that exists only on a duplicate-outcome record."""
    reg = tmp_path / "registry.jsonl"
    _register(reg, "acme-2025.pdf", CURR_ID, period_end="2025-12-31", source_hash="h25")
    _register(
        reg, "copy.pdf", PREV_ID, period_end="2024-12-31", source_hash="h24",
        parse_status=filing_registry.DUPLICATE, duplicate_of="somewhere.pdf",
    )
    with pytest.raises(ComparisonPairError) as excinfo:
        _create(reg, db)
    assert _reasons(excinfo) == ["previous_filing_not_parsed"]
    _assert_nothing_persisted(db)


def test_failed_registry_entry_rejected(tmp_path, db):
    """Test 12: a filing_id that exists only on a failed-outcome record."""
    reg = tmp_path / "registry.jsonl"
    _register(reg, "acme-2024.pdf", PREV_ID, period_end="2024-12-31", source_hash="h24")
    _register(
        reg, "broken.pdf", CURR_ID, period_end="2025-12-31", source_hash=None,
        parse_status=filing_registry.FAILED, error_code="ValueError",
    )
    with pytest.raises(ComparisonPairError) as excinfo:
        _create(reg, db)
    assert _reasons(excinfo) == ["current_filing_not_parsed"]
    _assert_nothing_persisted(db)


def test_conflicted_identity_rejected(reg, db, tmp_path):
    """Test 13: a parsed filing whose identity a conflict record disputes."""
    _register(
        reg, "restated.pdf", CURR_ID, period_end="2025-12-31",
        source_hash="other-content",
        parse_status=filing_registry.CONFLICT, conflict_with="acme-2025.pdf",
    )
    with pytest.raises(ComparisonPairError) as excinfo:
        _create(reg, db)
    assert _reasons(excinfo) == ["current_filing_identity_conflicted"]
    _assert_nothing_persisted(db)


def test_incomplete_reference_metadata_rejected(tmp_path, db):
    """Test 14: a parsed entry that cannot build a FilingReference without
    guessing (company_key missing) is rejected, not patched."""
    reg = tmp_path / "registry.jsonl"
    _register(reg, "acme-2025.pdf", CURR_ID, period_end="2025-12-31", source_hash="h25")
    filing_registry.record_outcome(
        reg,
        source_path="mystery.pdf",
        source_name="mystery.pdf",
        source_hash="hm",
        parse_status=filing_registry.PARSED,
        filing_id=PREV_ID,
        company_key=None,  # incomplete on purpose
        company_name=None,
        form_type="10-k",
        period_end="2024-12-31",
    )
    with pytest.raises(ComparisonPairError) as excinfo:
        _create(reg, db)
    assert _reasons(excinfo) == ["previous_filing_metadata_incomplete"]
    _assert_nothing_persisted(db)


def test_unsupported_and_empty_scope_rejected(reg, db):
    """Test 15 + empty scope: only v1-supported section keys are accepted."""
    with pytest.raises(ComparisonPairError) as excinfo:
        _create(reg, db, scope=["item_7_mdna"])
    assert _reasons(excinfo) == ["unsupported_section_scope"]

    with pytest.raises(ComparisonPairError) as excinfo:
        _create(reg, db, scope=["   "])
    assert _reasons(excinfo) == ["empty_section_scope"]
    _assert_nothing_persisted(db)


# --- Listing -----------------------------------------------------------------


def test_list_filters_by_filing_id_and_status(tmp_path, db):
    """Tests 18/19 at the store level: either-side filing filter and status."""
    reg = tmp_path / "registry.jsonl"
    _register(reg, "a-2023.pdf", "acme-corporation:10-k:2023-12-31",
              period_end="2023-12-31", source_hash="h23")
    _register(reg, "a-2024.pdf", PREV_ID, period_end="2024-12-31", source_hash="h24")
    _register(reg, "a-2025.pdf", CURR_ID, period_end="2025-12-31", source_hash="h25")

    first, _ = create_comparison(
        "acme-corporation:10-k:2023-12-31", PREV_ID, db_path=db, registry_path=reg
    )
    second, _ = create_comparison(PREV_ID, CURR_ID, db_path=db, registry_path=reg)

    everything = list_comparisons(db_path=db)
    assert {r["comparison_id"] for r in everything} == {
        first["comparison_id"], second["comparison_id"]
    }

    # PREV_ID appears on the current side of one and the previous side of the
    # other — the filter matches either side.
    both = list_comparisons(db_path=db, filing_id=PREV_ID)
    assert {r["comparison_id"] for r in both} == {
        first["comparison_id"], second["comparison_id"]
    }
    only_2025 = list_comparisons(db_path=db, filing_id=CURR_ID)
    assert [r["comparison_id"] for r in only_2025] == [second["comparison_id"]]

    ready = list_comparisons(db_path=db, status=STATUS_READY_FOR_DETECTION)
    assert len(ready) == 2
    assert list_comparisons(db_path=db, status="failed") == []
