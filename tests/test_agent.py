"""Tests for the agent module."""

import pytest

from agent import (
    SYSTEM_PROMPT,
    apply_external_fallback,
    build_agent,
    query,
    should_expose_retrieved_sources,
    should_run_external_fallback,
)


def test_build_agent():
    """Agent graph should build without errors."""
    graph = build_agent()
    assert graph is not None


def test_query_returns_output():
    """A simple query should return a non-empty output string."""
    result = query("What were Acme Corp's Q4 2025 earnings?")
    assert "output" in result
    assert len(result["output"]) > 0


def test_query_uses_local_search():
    """Agent should use local_search tool for document questions."""
    result = query("Summarize the cybersecurity risk factors")
    output = result["output"].lower()
    assert "cybersecurity" in output or "security" in output


def test_query_with_chat_history():
    """Query should work with prior chat history."""
    history = [
        ("human", "What is Acme Corp?"),
        ("ai", "Acme Corp is a financial services company."),
    ]
    result = query("What are their risk factors?", chat_history=history)
    assert "output" in result
    assert len(result["output"]) > 0


def test_output_contains_result_summary():
    """Agent output should follow the Result Summary format."""
    result = query("What does the compliance policy say about blackout periods?")
    output = result["output"]
    assert "Result Summary" in output
    assert "Internal Corpus Answer:" in output or "Internal Document Answer:" in output


def test_unavailable_company_not_substituted():
    """Asking about a company not in corpus should not return another company's data."""
    result = query("What are Tesla's risk factors from their latest 10-K?")
    output = result["output"]
    assert "unavailable" in output.lower() or "not in" in output.lower() or "not available" in output.lower()
    # Should not present Acme data as Tesla data
    assert "acme" not in output.lower().split("internal corpus answer")[0] if "internal corpus answer" in output.lower() else True


def test_prompt_allows_external_context_for_missing_company():
    """Missing-company questions may use web results without substituting corpus data."""
    prompt = SYSTEM_PROMPT.lower()
    assert "must use web_search" in prompt
    assert "external context" in prompt
    assert "company that is missing" in prompt


def test_unavailable_internal_answer_hides_ui_sources():
    """Rejected local chunks should remain auditable but not display as evidence."""
    output = (
        "## Result Summary\n\n"
        "Internal Corpus Answer: Unavailable — NVIDIA is not in local corpus.\n\n"
        "External Context: Partially available."
    )
    assert should_expose_retrieved_sources(output) is False


def test_available_internal_answer_exposes_ui_sources():
    """Used internal chunks should display in the answer view."""
    output = (
        "## Result Summary\n\n"
        "Internal Corpus Answer: Available in local corpus.\n\n"
        "External Context: Unavailable."
    )
    assert should_expose_retrieved_sources(output) is True


def test_latest_missing_company_triggers_external_fallback():
    """Public filing questions should run fallback web search if agent skipped it."""
    output = (
        "## Result Summary\n\n"
        "Internal Corpus Answer: Unavailable. NVIDIA is not in the local corpus.\n\n"
        "External Context: Unavailable. Web search not performed."
    )
    assert should_run_external_fallback(
        "What do NVIDIA's latest filings say about key risk factors?",
        output,
    ) is True


def test_non_public_missing_company_does_not_trigger_external_fallback():
    """Fallback should stay scoped to public/current/filing-style requests."""
    output = (
        "## Result Summary\n\n"
        "Internal Corpus Answer: Unavailable. NVIDIA is not in the local corpus.\n\n"
        "External Context: Unavailable."
    )
    assert should_run_external_fallback("Tell me about NVIDIA.", output) is False


def test_apply_external_fallback_replaces_external_context():
    """Fallback web results should replace the unavailable external section."""
    output = (
        "## Result Summary\n\n"
        '<span style="color: #2e7d32; font-weight: bold;">Internal Corpus Answer:</span> '
        "Unavailable. NVIDIA is not in the local corpus.\n\n"
        '<span style="color: #1565c0; font-style: italic;">External Context:</span> '
        "Unavailable. Web search not performed."
    )
    web_results = "- [sec.gov](https://www.sec.gov/) | SEC filing | Risk factors."

    updated = apply_external_fallback(output, web_results)

    assert "External Context:</span> Available." in updated
    assert "[sec.gov](https://www.sec.gov/)" in updated
    assert "Web search not performed" not in updated
