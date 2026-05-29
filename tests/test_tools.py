"""Tests for retrieval and web search tools."""

import pytest

from tools import (
    local_search,
    web_search,
    get_retriever,
    _get_vectorstore,
    _has_unknown_company_filter,
    _metadata_filter_from_query,
)


def test_local_search_returns_results():
    """local_search should find relevant chunks from the ingested corpus."""
    result = local_search.invoke("cybersecurity risk")
    assert isinstance(result, str)
    assert "cybersecurity" in result.lower() or "security" in result.lower()
    assert "[Source" in result


def test_local_search_includes_source_metadata():
    result = local_search.invoke("personal trading policy")
    assert "compliance-policy" in result.lower() or "Source" in result


def test_local_search_no_match():
    """Obscure query should still return something or a no-results message."""
    result = local_search.invoke("quantum entanglement in deep space")
    assert isinstance(result, str)
    assert len(result) > 0


def test_web_search_no_api_key(monkeypatch):
    """Without TAVILY_API_KEY, web_search should return a graceful message."""
    monkeypatch.setattr("config.TAVILY_API_KEY", None)
    result = web_search.invoke("latest SEC enforcement actions")
    assert "not available" in result.lower()


def test_web_search_returns_markdown_links():
    """Web search results should contain clickable markdown links."""
    import config
    if not config.TAVILY_API_KEY:
        pytest.skip("TAVILY_API_KEY not set")
    result = web_search.invoke("current Federal Reserve interest rate")
    assert "](http" in result, "Expected markdown link syntax in web results"


def test_web_search_no_date_unavailable_noise():
    """Web results should not contain 'date unavailable' strings."""
    import config
    if not config.TAVILY_API_KEY:
        pytest.skip("TAVILY_API_KEY not set")
    result = web_search.invoke("current Federal Reserve interest rate")
    assert "date unavailable" not in result.lower()


def test_retriever_returns_documents():
    retriever = get_retriever()
    docs = retriever.invoke("earnings")
    assert len(docs) > 0
    assert any("revenue" in d.page_content.lower() or "earnings" in d.page_content.lower() for d in docs)


def test_vectorstore_singleton():
    """Multiple calls to _get_vectorstore should return the same instance."""
    vs1 = _get_vectorstore()
    vs2 = _get_vectorstore()
    assert vs1 is vs2


def test_concurrent_local_search():
    """Parallel local_search calls should not raise (Chroma thread safety)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    queries = ["cybersecurity", "earnings", "compliance", "trading"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(local_search.invoke, q) for q in queries]
        results = [f.result() for f in as_completed(futures)]

    assert len(results) == 4
    assert all(isinstance(r, str) and len(r) > 0 for r in results)


def test_metadata_filter_from_query_infers_company_type_and_year(monkeypatch):
    monkeypatch.setattr("tools._available_company_keys", lambda: {"acme corporation"})

    where_filter = _metadata_filter_from_query(
        "What were Acme Corp's fiscal year 2025 revenue and earnings per share in the 10-K?"
    )

    assert where_filter == {
        "$and": [
            {"company_key": "acme corporation"},
            {"filing_type": "10-k"},
            {"year": 2025},
        ]
    }


def test_metadata_filter_from_query_marks_unknown_company(monkeypatch):
    monkeypatch.setattr("tools._available_company_keys", lambda: {"acme corporation"})

    where_filter = _metadata_filter_from_query("What do NVIDIA's latest filings say?")

    assert where_filter == {"company_key": "nvidia"}
    assert _has_unknown_company_filter(where_filter) is True
