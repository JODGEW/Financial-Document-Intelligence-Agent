"""Tests for bounded, deterministic Chroma upserts.

Everything here runs offline: a recording fake store for the batching contract,
and a real local Chroma with a deterministic stub embedding function for the
cases that must prove the *client's own* limit is respected. No AWS, no
Bedrock, no network.

The suite is organized as:
  1. batch-size discovery and validation
  2. payload validation
  3. deterministic partitioning and payload fidelity
  4. failure behavior and error hygiene
  5. real-client bounds (a payload the client refuses in one call)
  6. production ingestion wiring, registry completion ordering, and rerun
  7. shared-helper unification (no duplicate implementation survives)
"""

from __future__ import annotations

import ast
import functools
import hashlib
import io
import tokenize
from pathlib import Path

import pytest
from langchain_core.documents import Document

import chroma_batching
import config
import filing_registry
import ingest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _code_only(path: Path) -> str:
    """Source with comments and string literals removed.

    Prose that *describes* a forbidden construct ("this module never retries")
    must not satisfy or trip a structural check; only executable code counts.
    """
    source = path.read_text(encoding="utf-8")
    kept: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        kept.append(token.string)
    return " ".join(kept)


# --- Test doubles -------------------------------------------------------------


class _FakeClient:
    def __init__(self, limit):
        self._limit = limit

    def get_max_batch_size(self):
        if isinstance(self._limit, Exception):
            raise self._limit
        return self._limit


class _RecordingStore:
    """Records every add_documents call verbatim, in order.

    ``fail_on_batch`` is a 0-based batch index that raises instead of writing,
    so partial-completion behavior is exercised without corrupting a real store.
    """

    def __init__(self, limit=10, fail_on_batch=None, client=True):
        self.calls: list[tuple[list, list]] = []
        self.fail_on_batch = fail_on_batch
        if client:
            self._client = _FakeClient(limit)

    def add_documents(self, documents, ids):
        if self.fail_on_batch is not None and len(self.calls) == self.fail_on_batch:
            raise RuntimeError(
                "chroma internals: record 'sec-filing-2024.htm:12:abc' rejected "
                "at /Users/someone/private/chroma_db; INSERT INTO embeddings"
            )
        self.calls.append((list(documents), list(ids)))
        return list(ids)


class _StubEmbeddings:
    """Deterministic offline embeddings. Detection never embeds a query."""

    def embed_documents(self, texts):
        return [[float(len(text) % 7), 1.0] for text in texts]

    def embed_query(self, text):
        raise AssertionError("no query embedding in these tests")


def _payload(n, prefix="doc"):
    documents = [
        Document(page_content=f"{prefix} body {i}", metadata={"seq": i})
        for i in range(n)
    ]
    ids = [f"{prefix}-{i}" for i in range(n)]
    return documents, ids


def _real_store(tmp_path, name="batching"):
    from langchain_chroma import Chroma

    return Chroma(
        collection_name=name,
        persist_directory=str(tmp_path / "chroma"),
        embedding_function=_StubEmbeddings(),
    )


# --- 1. Batch-size discovery --------------------------------------------------


def test_batch_size_is_discovered_from_the_client(tmp_path):
    """Test 1: the installed client's own maximum is used, not a constant."""
    store = _real_store(tmp_path)
    reported = store._client.get_max_batch_size()
    assert isinstance(reported, int) and not isinstance(reported, bool)
    assert reported > 0
    assert chroma_batching.resolve_batch_size(store) == reported
    # The observed 5,461 is never baked into the source.
    source = (REPO_ROOT / "chroma_batching.py").read_text(encoding="utf-8")
    assert "5461" not in source.replace("5,461", "")


def test_boolean_reported_limit_is_rejected():
    """Test 2: bool is an int subclass; True must not become batch size 1."""
    with pytest.raises(chroma_batching.ChromaBatchConfigurationError) as excinfo:
        chroma_batching.resolve_batch_size(_RecordingStore(limit=True))
    assert excinfo.value.code == chroma_batching.CODE_INVALID_BATCH_SIZE


def test_zero_reported_limit_is_rejected():
    """Test 3."""
    with pytest.raises(chroma_batching.ChromaBatchConfigurationError) as excinfo:
        chroma_batching.resolve_batch_size(_RecordingStore(limit=0))
    assert excinfo.value.code == chroma_batching.CODE_INVALID_BATCH_SIZE


def test_negative_reported_limit_is_rejected():
    """Test 4."""
    with pytest.raises(chroma_batching.ChromaBatchConfigurationError) as excinfo:
        chroma_batching.resolve_batch_size(_RecordingStore(limit=-1))
    assert excinfo.value.code == chroma_batching.CODE_INVALID_BATCH_SIZE


def test_non_integer_reported_limit_is_rejected():
    """A float or a string is not a bound either."""
    for bad in (4096.0, "4096", None):
        store = _RecordingStore(limit=bad)
        with pytest.raises(chroma_batching.ChromaBatchConfigurationError):
            chroma_batching.resolve_batch_size(store)


def test_missing_client_capability_fails_closed():
    """Test 5: no fallback constant. A guessed bound is not a bound, so the
    write is refused rather than issued unbounded."""
    store = _RecordingStore(client=False)
    with pytest.raises(chroma_batching.ChromaBatchConfigurationError) as excinfo:
        chroma_batching.resolve_batch_size(store)
    assert excinfo.value.code == chroma_batching.CODE_BATCH_SIZE_UNAVAILABLE

    documents, ids = _payload(3)
    with pytest.raises(chroma_batching.ChromaBatchConfigurationError):
        chroma_batching.add_documents_in_batches(store, documents, ids)
    assert store.calls == []  # nothing was written


def test_raising_client_fails_closed():
    store = _RecordingStore(limit=RuntimeError("client is down"))
    with pytest.raises(chroma_batching.ChromaBatchConfigurationError) as excinfo:
        chroma_batching.resolve_batch_size(store)
    assert excinfo.value.code == chroma_batching.CODE_BATCH_SIZE_UNAVAILABLE
    assert "client is down" not in str(excinfo.value)


def test_explicit_batch_size_overrides_discovery():
    """Test 6."""
    store = _RecordingStore(limit=1000)
    assert chroma_batching.resolve_batch_size(store, explicit=7) == 7
    documents, ids = _payload(10)
    result = chroma_batching.add_documents_in_batches(
        store, documents, ids, batch_size=4
    )
    assert result.effective_batch_size == 4
    assert result.batch_count == 3


def test_invalid_explicit_batch_size_is_rejected():
    store = _RecordingStore(limit=1000)
    for bad in (0, -5, True, 3.5, "4"):
        with pytest.raises(chroma_batching.ChromaBatchConfigurationError) as excinfo:
            chroma_batching.resolve_batch_size(store, explicit=bad)
        assert excinfo.value.code == chroma_batching.CODE_INVALID_BATCH_SIZE


# --- 2. Payload validation ----------------------------------------------------


def test_unequal_payload_lengths_are_rejected_before_any_write():
    """Test 7."""
    store = _RecordingStore()
    documents, ids = _payload(5)
    with pytest.raises(chroma_batching.ChromaBatchConfigurationError) as excinfo:
        chroma_batching.add_documents_in_batches(store, documents, ids[:4])
    assert excinfo.value.code == chroma_batching.CODE_PAYLOAD_LENGTH_MISMATCH
    assert store.calls == []


def test_empty_or_non_string_ids_are_rejected():
    store = _RecordingStore()
    documents, ids = _payload(3)
    for bad in ("", None, 7):
        broken = list(ids)
        broken[1] = bad
        with pytest.raises(chroma_batching.ChromaBatchConfigurationError) as excinfo:
            chroma_batching.add_documents_in_batches(store, documents, broken)
        assert excinfo.value.code == chroma_batching.CODE_INVALID_ID
    assert store.calls == []


def test_duplicate_ids_are_not_rejected():
    """The helper does not impose a new id policy: deduplication is the
    caller's contract (ingest._dedupe_by_id), unchanged by this module."""
    store = _RecordingStore()
    documents, ids = _payload(4)
    ids[3] = ids[0]
    result = chroma_batching.add_documents_in_batches(
        store, documents, ids, batch_size=10
    )
    assert result.total_items == 4


# --- 3. Deterministic partitioning and fidelity -------------------------------


def _written(store):
    """Flatten every recorded call back into (documents, ids)."""
    documents = [doc for call in store.calls for doc in call[0]]
    ids = [item for call in store.calls for item in call[1]]
    return documents, ids


def test_small_input_uses_one_batch():
    """Test 8."""
    store = _RecordingStore()
    documents, ids = _payload(3)
    result = chroma_batching.add_documents_in_batches(
        store, documents, ids, batch_size=10
    )
    assert result.batch_count == 1
    assert len(store.calls) == 1


def test_exact_limit_input_uses_one_full_batch():
    """Test 9."""
    store = _RecordingStore()
    documents, ids = _payload(10)
    result = chroma_batching.add_documents_in_batches(
        store, documents, ids, batch_size=10
    )
    assert result.batch_count == 1
    assert [len(call[1]) for call in store.calls] == [10]


def test_limit_plus_one_uses_two_batches_with_a_single_item_tail():
    """Test 10."""
    store = _RecordingStore()
    documents, ids = _payload(11)
    result = chroma_batching.add_documents_in_batches(
        store, documents, ids, batch_size=10
    )
    assert result.batch_count == 2
    assert [len(call[1]) for call in store.calls] == [10, 1]


def test_multiple_full_batches():
    """Test 11."""
    store = _RecordingStore()
    documents, ids = _payload(30)
    result = chroma_batching.add_documents_in_batches(
        store, documents, ids, batch_size=10
    )
    assert result.batch_count == 3
    assert [len(call[1]) for call in store.calls] == [10, 10, 10]


def test_final_partial_batch():
    """Test 12."""
    store = _RecordingStore()
    documents, ids = _payload(25)
    chroma_batching.add_documents_in_batches(store, documents, ids, batch_size=10)
    assert [len(call[1]) for call in store.calls] == [10, 10, 5]


def test_batches_are_contiguous_non_overlapping_and_complete():
    """Boundaries are items[k*size:(k+1)*size] — no gap, no repeat."""
    store = _RecordingStore()
    documents, ids = _payload(23)
    chroma_batching.add_documents_in_batches(store, documents, ids, batch_size=7)
    _, written_ids = _written(store)
    assert written_ids == ids
    assert len(written_ids) == len(set(written_ids))


def test_input_ordering_is_preserved():
    """Test 13."""
    store = _RecordingStore()
    documents, ids = _payload(37)
    chroma_batching.add_documents_in_batches(store, documents, ids, batch_size=5)
    written_docs, written_ids = _written(store)
    assert written_ids == ids
    assert [doc.metadata["seq"] for doc in written_docs] == list(range(37))


def test_ids_are_preserved_byte_for_byte():
    """Test 14."""
    store = _RecordingStore()
    documents, _ = _payload(12)
    ids = [f"file name {i}.htm:{i}:{'ü' * (i % 3)}{i:012d}" for i in range(12)]
    chroma_batching.add_documents_in_batches(store, documents, ids, batch_size=5)
    _, written_ids = _written(store)
    assert written_ids == ids


def test_documents_and_metadata_are_preserved_exactly():
    """Tests 15-16: identity-preserving, no copying, no normalization."""
    store = _RecordingStore()
    documents = [
        Document(
            page_content=f"  Item 1A. Risk Factors  {i}\n\n",
            metadata={"section_key": "item_1a", "chunk_seq": i, "page": i % 4},
        )
        for i in range(9)
    ]
    ids = [f"id-{i}" for i in range(9)]
    originals = [(doc.page_content, dict(doc.metadata)) for doc in documents]
    chroma_batching.add_documents_in_batches(store, documents, ids, batch_size=4)
    written_docs, _ = _written(store)
    assert [doc is documents[i] for i, doc in enumerate(written_docs)] == [True] * 9
    for doc, (text, metadata) in zip(written_docs, originals):
        assert doc.page_content == text
        assert doc.metadata == metadata


def test_empty_input_writes_nothing_and_returns_a_zero_result():
    """Test 17."""
    store = _RecordingStore(limit=64)
    result = chroma_batching.add_documents_in_batches(store, [], [], batch_size=None)
    assert store.calls == []
    assert result == chroma_batching.BatchWriteResult(
        total_items=0, batch_count=0, effective_batch_size=64, completed_items=0
    )


@pytest.mark.parametrize("total", [1, 9, 10, 11, 19, 20, 21, 100])
def test_no_batch_ever_exceeds_the_effective_limit(total):
    """Test 18."""
    store = _RecordingStore()
    documents, ids = _payload(total)
    result = chroma_batching.add_documents_in_batches(
        store, documents, ids, batch_size=10
    )
    assert all(len(call[1]) <= 10 for call in store.calls)
    assert result.completed_items == total
    assert result.total_items == total


def test_result_exposes_counts_only():
    """The return value carries no documents, metadata, ids, or client state."""
    store = _RecordingStore()
    documents, ids = _payload(5)
    result = chroma_batching.add_documents_in_batches(
        store, documents, ids, batch_size=2
    )
    assert set(vars(result)) == {
        "total_items",
        "batch_count",
        "effective_batch_size",
        "completed_items",
    }


def test_helper_does_not_retry_sleep_or_parallelize():
    """Checked at the source: a retry loop or a sleep here would silently
    change ingestion's failure semantics."""
    source = (REPO_ROOT / "chroma_batching.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {
        "time",
        "asyncio",
        "threading",
        "concurrent",
        "socket",
        "requests",
        "httpx",
        "urllib",
        "boto3",
        "sqlite3",
        "subprocess",
    }
    assert imported & forbidden == set(), imported & forbidden
    code = _code_only(REPO_ROOT / "chroma_batching.py")
    for name in ("sleep", "retry", "backoff", "ThreadPool", "Executor"):
        assert name not in code, name


# --- 4. Failure behavior and error hygiene ------------------------------------


def test_failure_in_the_first_batch_reports_zero_completed():
    """Test 23."""
    store = _RecordingStore(fail_on_batch=0)
    documents, ids = _payload(25)
    with pytest.raises(chroma_batching.ChromaBatchWriteError) as excinfo:
        chroma_batching.add_documents_in_batches(
            store, documents, ids, batch_size=10
        )
    error = excinfo.value
    assert error.completed_items == 0
    assert error.batch_index == 0
    assert store.calls == []


def test_failure_in_a_later_batch_reports_the_accurate_completed_count():
    """Test 24: batches 1-2 landed, batch 3 did not."""
    store = _RecordingStore(fail_on_batch=2)
    documents, ids = _payload(25)
    with pytest.raises(chroma_batching.ChromaBatchWriteError) as excinfo:
        chroma_batching.add_documents_in_batches(
            store, documents, ids, batch_size=10
        )
    error = excinfo.value
    assert error.completed_items == 20
    assert error.total_items == 25
    assert error.batch_index == 2
    assert error.batch_count == 3
    assert error.effective_batch_size == 10
    assert error.code == chroma_batching.CODE_WRITE_FAILED
    # Earlier batches are still physically present: writes are not a transaction.
    _, written_ids = _written(store)
    assert written_ids == ids[:20]


def test_write_error_text_leaks_no_content_paths_or_client_message():
    """Test 36. The fake client's message deliberately contains a chunk id, an
    absolute path, and SQL; none of it may reach the raised error."""
    store = _RecordingStore(fail_on_batch=1)
    documents, ids = _payload(15)
    with pytest.raises(chroma_batching.ChromaBatchWriteError) as excinfo:
        chroma_batching.add_documents_in_batches(
            store, documents, ids, batch_size=10
        )
    message = str(excinfo.value)
    for leak in (
        "sec-filing-2024.htm",
        "/Users/",
        "INSERT INTO",
        "chroma_db",
        "doc body",
        "doc-1",
        "chroma internals",
    ):
        assert leak not in message, leak
    # A class name is safe and useful; a message is not.
    assert excinfo.value.client_error_type == "RuntimeError"
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    assert chroma_batching.CODE_WRITE_FAILED in message


def test_configuration_errors_carry_stable_codes():
    """Stable codes rather than client wording, so logs and tests can rely on
    them across client versions."""
    assert chroma_batching.CODE_WRITE_FAILED == "chroma_batch_write_failed"
    assert (
        chroma_batching.CODE_PAYLOAD_LENGTH_MISMATCH
        == "chroma_batch_payload_length_mismatch"
    )
    assert chroma_batching.CODE_INVALID_ID == "chroma_batch_invalid_id"
    assert chroma_batching.CODE_INVALID_BATCH_SIZE == "chroma_batch_invalid_batch_size"
    assert (
        chroma_batching.CODE_BATCH_SIZE_UNAVAILABLE == "chroma_batch_size_unavailable"
    )
    assert issubclass(
        chroma_batching.ChromaBatchConfigurationError, chroma_batching.ChromaBatchError
    )
    assert issubclass(
        chroma_batching.ChromaBatchWriteError, chroma_batching.ChromaBatchError
    )


# --- 5. Real-client bounds ----------------------------------------------------


def test_real_client_refuses_an_unbounded_over_limit_write(tmp_path):
    """The defect this change removes, reproduced against the real client."""
    store = _real_store(tmp_path, name="unbounded")
    limit = store._client.get_max_batch_size()
    documents, ids = _payload(limit + 1, prefix="over")
    with pytest.raises(Exception):
        store.add_documents(documents=documents, ids=ids)


def test_over_limit_payload_succeeds_through_bounded_writes(tmp_path):
    """Test 22 (helper level): more items than the client accepts in one call,
    written through the helper, all present afterwards and in order."""
    store = _real_store(tmp_path, name="bounded")
    limit = store._client.get_max_batch_size()
    total = limit + 1
    documents, ids = _payload(total, prefix="bounded")
    result = chroma_batching.add_documents_in_batches(store, documents, ids)
    assert result.effective_batch_size == limit
    assert result.batch_count == 2
    assert result.completed_items == total
    assert store._collection.count() == total
    stored = store._collection.get(ids=ids[:5])
    assert set(stored["ids"]) == set(ids[:5])


def test_rerunning_an_over_limit_write_does_not_duplicate(tmp_path):
    """Test 27 (helper level): deterministic ids upsert, they do not append."""
    store = _real_store(tmp_path, name="rerun")
    limit = store._client.get_max_batch_size()
    documents, ids = _payload(limit + 1, prefix="rerun")
    chroma_batching.add_documents_in_batches(store, documents, ids)
    first = store._collection.count()
    chroma_batching.add_documents_in_batches(store, documents, ids)
    assert store._collection.count() == first


# --- 6. Production ingestion wiring ------------------------------------------


def _md_corpus(tmp_path, name="acme-corp-10k-2025.md", body=None):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(exist_ok=True)
    (docs_dir / name).write_text(
        body or "# FORM 10-K\n\nAnnual report narrative with enough content.\n",
        encoding="utf-8",
    )
    return docs_dir


def _manifest_for(name, period_end="2025-12-31"):
    from datetime import date

    return {
        name: {
            "company_name": "Acme Corporation",
            "form_type": "10-k",
            "period_end": date.fromisoformat(period_end),
            "filing_date": date.fromisoformat("2026-02-19"),
        }
    }


@pytest.fixture
def ingestion_env(tmp_path, monkeypatch):
    """ingest.run() against a temporary corpus, registry, and local Chroma.

    BedrockEmbeddings is replaced by the deterministic stub: this exercises the
    real ingestion path with no credentials and no network.
    """
    docs_dir = _md_corpus(tmp_path)
    registry_path = tmp_path / "registry.jsonl"
    # load_documents binds config.DOCS_DIR as a default argument at import
    # time, so the corpus is redirected by binding the positional instead.
    monkeypatch.setattr(
        ingest,
        "load_documents",
        functools.partial(ingest.load_documents, str(docs_dir)),
    )
    monkeypatch.setattr(config, "DOCS_DIR", str(docs_dir))
    monkeypatch.setattr(config, "FILING_REGISTRY_PATH", str(registry_path))
    monkeypatch.setattr(config, "CHROMA_PERSIST_DIR", str(tmp_path / "chroma"))
    monkeypatch.setattr(config, "CHROMA_COLLECTION", "ingest_batching")
    monkeypatch.setattr(ingest, "BedrockEmbeddings", lambda **kwargs: _StubEmbeddings())
    monkeypatch.setattr(
        filing_registry, "load_manifest", lambda *a, **k: _manifest_for(
            "acme-corp-10k-2025.md"
        )
    )
    return {"docs_dir": docs_dir, "registry_path": registry_path, "tmp_path": tmp_path}


def _entry(registry_path, source_path="acme-corp-10k-2025.md"):
    for item in filing_registry.list_entries(registry_path):
        if item["source_path"] == source_path:
            return item
    raise AssertionError(f"no registry entry for {source_path}")


def test_production_ingestion_uses_the_shared_helper(ingestion_env, monkeypatch):
    """Test 20: ingest.py writes through chroma_batching, not add_documents."""
    seen = {}
    real = chroma_batching.add_documents_in_batches

    def spy(store, documents, ids, **kwargs):
        seen["operation"] = kwargs.get("operation")
        seen["count"] = len(documents)
        return real(store, documents, ids, **kwargs)

    monkeypatch.setattr(chroma_batching, "add_documents_in_batches", spy)
    ingest.run()
    assert seen["operation"] == "ingest.embed_and_persist"
    assert seen["count"] > 0
    source = (REPO_ROOT / "ingest.py").read_text(encoding="utf-8")
    assert ".add_documents(" not in source


def test_ingestion_records_chunk_count_only_after_the_write_succeeds(ingestion_env):
    """Test 25 (success half) plus the completion-ordering contract."""
    ingest.run()
    entry = _entry(ingestion_env["registry_path"])
    assert entry["parse_status"] == filing_registry.PARSED
    assert entry["chunk_count"] and entry["chunk_count"] > 0


def test_registry_completion_is_written_after_the_last_batch(
    ingestion_env, monkeypatch
):
    """The ordering itself: at the moment of every Chroma write, the registry
    must not yet be claiming a complete ingestion.

    Observed on a *second* run, when a completion marker from the first run
    already exists — a fresh registry would satisfy this trivially.
    """
    registry_path = ingestion_env["registry_path"]
    ingest.run()
    assert _entry(registry_path)["chunk_count"] > 0

    observed: list = []
    real = chroma_batching.add_documents_in_batches

    def spy(store, documents, ids, **kwargs):
        entries = filing_registry.list_entries(registry_path)
        observed.append([item.get("chunk_count") for item in entries])
        return real(store, documents, ids, **kwargs)

    monkeypatch.setattr(chroma_batching, "add_documents_in_batches", spy)
    ingest.run()
    assert observed, "the helper was never called"
    for snapshot in observed:
        assert all(count is None for count in snapshot), snapshot
    assert _entry(registry_path)["chunk_count"] > 0


def test_failed_write_leaves_the_filing_incomplete_and_exits_nonzero(
    ingestion_env, monkeypatch, capsys
):
    """Tests 23/25: a failing write must not produce a completion claim."""

    def boom(*args, **kwargs):
        raise chroma_batching.ChromaBatchWriteError(
            "chroma_batch_write_failed: ingest.embed_and_persist failed on "
            "batch 2 of 3 after 10 of 25 items (batch size 10, client error "
            "RuntimeError)",
            operation="ingest.embed_and_persist",
            batch_index=1,
            batch_count=3,
            total_items=25,
            completed_items=10,
            effective_batch_size=10,
            client_error_type="RuntimeError",
        )

    monkeypatch.setattr(chroma_batching, "add_documents_in_batches", boom)
    with pytest.raises(SystemExit) as excinfo:
        ingest.run()
    assert excinfo.value.code == 1
    entry = _entry(ingestion_env["registry_path"])
    assert entry["chunk_count"] is None
    out = capsys.readouterr().out
    assert "chroma_batch_write_failed" in out
    assert "not transactional" in out
    for leak in ("INSERT INTO", "Traceback"):
        assert leak not in out


def test_failed_write_clears_a_stale_completion_marker(ingestion_env, monkeypatch):
    """A previously complete filing must not keep claiming completeness while
    a later run rewrites its chunks and fails."""
    registry_path = ingestion_env["registry_path"]
    ingest.run()
    assert _entry(registry_path)["chunk_count"] > 0

    def boom(*args, **kwargs):
        raise chroma_batching.ChromaBatchWriteError(
            "chroma_batch_write_failed: failed",
            operation="ingest.embed_and_persist",
            batch_index=0,
            batch_count=1,
            total_items=1,
            completed_items=0,
            effective_batch_size=1,
            client_error_type="RuntimeError",
        )

    monkeypatch.setattr(chroma_batching, "add_documents_in_batches", boom)
    with pytest.raises(SystemExit):
        ingest.run()
    assert _entry(registry_path)["chunk_count"] is None


def test_explicit_rerun_after_a_partial_write_completes(ingestion_env, monkeypatch):
    """Tests 26/27: the documented recovery path. Batch 1 lands, batch 2 fails,
    the rerun upserts the same deterministic ids and finishes."""
    registry_path = ingestion_env["registry_path"]
    # A corpus large enough to need several small batches.
    body = "# FORM 10-K\n\n" + "\n\n".join(
        f"## Section {i}\n\nRisk narrative paragraph {i}. " * 12 for i in range(40)
    )
    (ingestion_env["docs_dir"] / "acme-corp-10k-2025.md").write_text(
        body, encoding="utf-8"
    )

    calls = {"n": 0}
    real = chroma_batching.add_documents_in_batches

    def failing_second_batch(store, documents, ids, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            half = max(1, len(documents) // 2)
            real(store, documents[:half], ids[:half], batch_size=5)
            raise chroma_batching.ChromaBatchWriteError(
                "chroma_batch_write_failed: injected",
                operation="ingest.embed_and_persist",
                batch_index=1,
                batch_count=2,
                total_items=len(documents),
                completed_items=half,
                effective_batch_size=5,
                client_error_type="RuntimeError",
            )
        return real(store, documents, ids, **kwargs)

    monkeypatch.setattr(
        chroma_batching, "add_documents_in_batches", failing_second_batch
    )
    with pytest.raises(SystemExit):
        ingest.run()
    assert _entry(registry_path)["chunk_count"] is None

    ingest.run()  # explicit rerun, same deterministic ids
    entry = _entry(registry_path)
    assert entry["chunk_count"] > 0

    from langchain_chroma import Chroma

    store = Chroma(
        collection_name="ingest_batching",
        persist_directory=config.CHROMA_PERSIST_DIR,
        embedding_function=_StubEmbeddings(),
    )
    assert store._collection.count() == entry["chunk_count"]


def test_repeated_identical_ingestion_produces_no_duplicate_chunks(ingestion_env):
    """Tests 1/2 of the concurrency section: the supported boundary is repeated
    explicit ingestion, which must converge, not accumulate."""
    from langchain_chroma import Chroma

    registry_path = ingestion_env["registry_path"]
    ingest.run()
    first_count = _entry(registry_path)["chunk_count"]
    first_entries = len(filing_registry.list_entries(registry_path))

    ingest.run()
    entry = _entry(registry_path)
    assert entry["chunk_count"] == first_count
    assert len(filing_registry.list_entries(registry_path)) == first_entries

    store = Chroma(
        collection_name="ingest_batching",
        persist_directory=config.CHROMA_PERSIST_DIR,
        embedding_function=_StubEmbeddings(),
    )
    assert store._collection.count() == first_count


def test_chunk_ids_ordering_and_metadata_are_unchanged_by_batching(ingestion_env):
    """Tests 30-33: batching is a delivery detail. Chunk ids, order, metadata,
    and the source hash are identical to what the unbatched path produced."""
    docs_dir = ingestion_env["docs_dir"]
    documents = ingest.load_documents()  # already bound to the temp corpus
    chunks = ingest.split_documents(documents)
    unique, ids = ingest._dedupe_by_id(chunks)

    # Deterministic id shape is unchanged: source:page:sha1(content)[:12].
    for chunk, chunk_id in zip(unique, ids):
        name = Path(chunk.metadata["source"]).name
        page = chunk.metadata.get("page", "")
        digest = hashlib.sha1(chunk.page_content.encode("utf-8")).hexdigest()[:12]
        assert chunk_id == f"{name}:{page}:{digest}"

    ingest.run()
    entry = _entry(ingestion_env["registry_path"])
    expected_hash = hashlib.sha256(
        (docs_dir / "acme-corp-10k-2025.md").read_bytes()
    ).hexdigest()
    assert entry["source_hash"] == expected_hash

    from langchain_chroma import Chroma

    store = Chroma(
        collection_name="ingest_batching",
        persist_directory=config.CHROMA_PERSIST_DIR,
        embedding_function=_StubEmbeddings(),
    )
    stored = store._collection.get(ids=ids)
    assert set(stored["ids"]) == set(ids)
    by_id = dict(zip(stored["ids"], stored["documents"]))
    by_id_meta = dict(zip(stored["ids"], stored["metadatas"]))
    for chunk, chunk_id in zip(unique, ids):
        assert by_id[chunk_id] == chunk.page_content
        assert by_id_meta[chunk_id]["chunk_seq"] == chunk.metadata["chunk_seq"]


def test_large_corpus_ingestion_exceeds_the_client_limit_and_succeeds(
    ingestion_env, monkeypatch
):
    """Test 22: a synthetic document producing more chunks than the client
    accepts in one call ingests successfully through bounded writes.

    Chunk volume is reached with a small explicit batch size rather than a
    5.8k-chunk document so the case stays fast; the over-limit real-client
    behavior is proven separately against the live client above.
    """
    from langchain_chroma import Chroma

    body = "# FORM 10-K\n\n" + "\n\n".join(
        f"## Section {i}\n\nRisk narrative paragraph {i}. " * 20 for i in range(120)
    )
    (ingestion_env["docs_dir"] / "acme-corp-10k-2025.md").write_text(
        body, encoding="utf-8"
    )

    batches: list[int] = []
    real = chroma_batching.add_documents_in_batches

    def bounded(store, documents, ids, **kwargs):
        kwargs["batch_size"] = 25
        result = real(store, documents, ids, **kwargs)
        batches.append(result.batch_count)
        return result

    monkeypatch.setattr(chroma_batching, "add_documents_in_batches", bounded)
    ingest.run()

    assert batches and batches[0] > 1, "the corpus did not need multiple batches"
    entry = _entry(ingestion_env["registry_path"])
    store = Chroma(
        collection_name="ingest_batching",
        persist_directory=config.CHROMA_PERSIST_DIR,
        embedding_function=_StubEmbeddings(),
    )
    assert store._collection.count() == entry["chunk_count"]


def test_duplicate_and_conflict_behavior_is_unchanged(ingestion_env, monkeypatch):
    """Tests 28-29: batching does not touch registry outcome resolution."""
    from datetime import date

    docs_dir = ingestion_env["docs_dir"]
    registry_path = ingestion_env["registry_path"]
    original = (docs_dir / "acme-corp-10k-2025.md").read_bytes()

    # Byte-identical copy under a second name -> duplicate, not indexed twice.
    (docs_dir / "acme-corp-10k-2025-copy.md").write_bytes(original)
    ingest.run()
    assert _entry(registry_path, "acme-corp-10k-2025-copy.md")["parse_status"] == (
        filing_registry.DUPLICATE
    )

    # Same filing identity, different content -> conflict, not indexed.
    (docs_dir / "acme-corp-10k-2025-copy.md").write_text(
        "# FORM 10-K\n\nDifferent annual report narrative entirely.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        filing_registry,
        "load_manifest",
        lambda *a, **k: {
            "acme-corp-10k-2025.md": {
                "company_name": "Acme Corporation",
                "form_type": "10-k",
                "period_end": date.fromisoformat("2025-12-31"),
                "filing_date": date.fromisoformat("2026-02-19"),
            },
            "acme-corp-10k-2025-copy.md": {
                "company_name": "Acme Corporation",
                "form_type": "10-k",
                "period_end": date.fromisoformat("2025-12-31"),
                "filing_date": date.fromisoformat("2026-02-19"),
            },
        },
    )
    ingest.run()
    statuses = {
        item["source_path"]: item["parse_status"]
        for item in filing_registry.list_entries(registry_path)
    }
    assert filing_registry.CONFLICT in statuses.values()


# --- 7. Shared-helper unification ---------------------------------------------


def test_no_source_module_issues_an_unbounded_vector_write():
    """Test 21 plus the audit invariant: every ingestion-path write in
    production and script code goes through the shared helper."""
    targets = [
        REPO_ROOT / "ingest.py",
        REPO_ROOT / "scripts" / "build_real_filing_benchmark.py",
        REPO_ROOT / "scripts" / "eval_comparison_regression.py",
    ]
    for path in targets:
        code = _code_only(path)
        assert ". add_documents (" not in code, path.name
        assert "add_documents_in_batches" in code, path.name


def test_benchmark_builder_uses_the_shared_helper_and_keeps_no_duplicate():
    """Test 19 and 21: the benchmark-local batching helper is gone."""
    source = (
        REPO_ROOT / "scripts" / "build_real_filing_benchmark.py"
    ).read_text(encoding="utf-8")
    assert "import chroma_batching" in source
    assert "add_documents_in_batches" in source
    assert "_add_in_batches" not in source
    assert "_CHROMA_FALLBACK_BATCH" not in source
    # And no second implementation anywhere in tracked source.
    for path in REPO_ROOT.glob("*.py"):
        if path.name == "chroma_batching.py":
            continue
        assert "get_max_batch_size" not in path.read_text(encoding="utf-8"), path.name
    for path in (REPO_ROOT / "scripts").glob("*.py"):
        assert "get_max_batch_size" not in path.read_text(encoding="utf-8"), path.name


def test_benchmark_builder_records_completion_after_the_write():
    """The same ordering as production, checked structurally: chunk counts are
    written after the bounded upsert, not before it."""
    source = (
        REPO_ROOT / "scripts" / "build_real_filing_benchmark.py"
    ).read_text(encoding="utf-8")
    write_at = source.index("add_documents_in_batches(")
    counts_at = source.index("update_chunk_counts(")
    assert write_at < counts_at


def test_this_suite_is_in_the_required_check():
    """A required check that does not run this suite cannot block a regression
    in it. Pinned here, and again in tests/test_comparison_reliability.py."""
    import yaml

    workflow_path = REPO_ROOT / ".github" / "workflows" / "comparison-regression.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    # The branch-protection contract is untouched by adding a step.
    assert workflow["name"] == "comparison-regression"
    assert list(workflow["jobs"]) == ["comparison-regression"]
    job = workflow["jobs"]["comparison-regression"]
    assert job["name"] == "comparison-regression"
    runs = "\n".join(
        step["run"]
        for step in job["steps"]
        if isinstance(step.get("run"), str) and "pytest" in step["run"]
    )
    assert "tests/test_chroma_batching.py" in runs
    # Still credential-free and still offline.
    raw = workflow_path.read_text(encoding="utf-8")
    for forbidden in ("secrets.", "AWS_ACCESS_KEY", "configure-aws-credentials"):
        assert forbidden not in raw, forbidden
    assert "env" not in job
    for step in job["steps"]:
        assert "env" not in step


def test_helper_is_importable_without_a_vector_store_or_credentials():
    """The module must stay dependency-light: it takes a store, it never
    constructs one, and it never reaches a model provider."""
    code = _code_only(REPO_ROOT / "chroma_batching.py")
    for name in ("chromadb", "langchain", "BedrockEmbeddings", "https"):
        assert name not in code, name
    # It never constructs a store either — the store is always injected.
    tree = ast.parse((REPO_ROOT / "chroma_batching.py").read_text(encoding="utf-8"))
    constructed = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "Chroma" not in constructed
