"""Tests for the evaluation runner and metrics."""

from types import SimpleNamespace

import pytest

from eval_runner import (
    EvalCase,
    ExpectedSource,
    citation_accuracy,
    evaluate_case,
    grounded_answer,
    load_eval_cases,
    retrieval_hit,
    summarize_results,
    tool_routing_hit,
    unsupported_claim_rate,
)


def test_load_eval_cases_validates_required_fields(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    dataset.write_text(
        '{"id":"case_1","question":"What is policy?","workflow_type":"local_only",'
        '"expected_tools":["local_search"],'
        '"expected_sources":[{"source_name":"policy.md"}],'
        '"expected_terms":["policy"]}\n'
    )

    cases = load_eval_cases(dataset)

    assert len(cases) == 1
    assert cases[0].id == "case_1"
    assert cases[0].expected_sources[0].source_name == "policy.md"


def test_load_eval_cases_rejects_malformed_rows(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    dataset.write_text('{"id":"case_1","question":"Missing fields"}\n')

    with pytest.raises(ValueError, match="workflow_type"):
        load_eval_cases(dataset)


def test_retrieval_hit_requires_all_expected_sources():
    expected_sources = [
        ExpectedSource("filing.pdf", page=1),
        ExpectedSource("policy.md"),
    ]
    retrieved_sources = [
        {"source_name": "filing.pdf", "page": 1},
        {"source_name": "policy.md", "page": None},
    ]

    assert retrieval_hit(expected_sources, retrieved_sources) is True
    assert retrieval_hit([ExpectedSource("filing.pdf", page=2)], retrieved_sources) is False


def test_citation_accuracy_checks_file_and_page():
    answer = "Source: filing.pdf, page 1. Also see policy.md."
    retrieved_sources = [
        {"source_name": "filing.pdf", "page": 1},
        {"source_name": "policy.md", "page": None},
    ]

    assert citation_accuracy(answer, retrieved_sources) == 1.0
    assert citation_accuracy("Source: filing.pdf, page 3.", retrieved_sources) == 0.0


def test_unsupported_claim_rate_checks_numeric_claims_against_evidence():
    answer = "Revenue was $130.5 billion and EPS was 2.94."
    evidence = "The company reported $130.5 billion in revenue."

    assert unsupported_claim_rate(answer, evidence) == 0.5


def test_grounded_answer_requires_terms_and_supported_claims():
    answer = "Revenue was $130.5 billion."
    evidence = "Revenue was $130.5 billion."

    assert grounded_answer(answer, ["revenue"], evidence) is True
    assert grounded_answer(answer, ["earnings"], evidence) is False
    assert grounded_answer("Revenue was $999 billion.", ["revenue"], evidence) is False


def test_tool_routing_hit_detects_expected_tool_calls():
    messages = [
        SimpleNamespace(
            type="ai",
            content="",
            tool_calls=[
                {"id": "call-1", "name": "local_search", "args": {"query": "risk"}}
            ],
        ),
        SimpleNamespace(type="tool", tool_call_id="call-1", name=None, content="result"),
    ]

    assert tool_routing_hit(["local_search"], messages) is True
    assert tool_routing_hit(["web_search"], messages) is False


def test_evaluate_case_computes_metrics_with_injected_dependencies():
    case = EvalCase(
        id="case_1",
        question="What was revenue?",
        workflow_type="local_only",
        expected_tools=["local_search"],
        expected_sources=[ExpectedSource("filing.pdf", page=1)],
        expected_terms=["revenue"],
    )
    docs = [
        SimpleNamespace(
            page_content="Revenue was $130.5 billion.",
            metadata={"source": "/repo/docs/filing.pdf", "page": 1},
        )
    ]

    def fake_query(_question):
        return {
            "output": "Revenue was $130.5 billion. Source: filing.pdf, page 1.",
            "sources": [
                {
                    "source_name": "filing.pdf",
                    "page": 1,
                    "excerpt": "Revenue was $130.5 billion.",
                }
            ],
            "messages": [
                {
                    "type": "tool",
                    "name": "local_search",
                    "content": "Revenue was $130.5 billion.",
                }
            ],
        }

    result = evaluate_case(case, run_query=fake_query, retrieve=lambda _question: docs)

    assert result.retrieval_hit is True
    assert result.grounded_answer is True
    assert result.citation_accuracy == 1.0
    assert result.tool_routing_hit is True
    assert result.latency_seconds >= 0


def test_evaluate_case_fallback_skips_local_grounding_metrics():
    case = EvalCase(
        id="nvidia_missing",
        question="What do NVIDIA's filings say?",
        workflow_type="missing_company_fallback",
        expected_tools=["local_search", "web_search"],
        expected_sources=[],
        expected_terms=["NVIDIA", "risk"],
    )
    distractor_docs = [
        SimpleNamespace(
            page_content="Acme Corp revenue was $284.7 million.",
            metadata={"source": "/repo/docs/acme.pdf", "page": 1},
        )
    ]

    def fake_query(_question):
        return {
            "output": (
                "Internal Corpus Answer: NVIDIA is not in local corpus. "
                "External Context: Available. NVIDIA risk factors include export controls."
            ),
            "sources": [],
            "messages": [
                {"type": "tool", "name": "local_search", "content": ""},
                {"type": "tool", "name": "web_search", "content": ""},
            ],
        }

    result = evaluate_case(case, run_query=fake_query, retrieve=lambda _q: distractor_docs)

    assert result.unsupported_claim_rate is None
    assert result.citation_accuracy is None
    assert result.local_refusal_correct is True
    assert result.grounded_answer is True
    assert result.tool_routing_hit is True


def test_summarize_results_includes_latency_by_workflow_type():
    case = EvalCase(
        id="case_1",
        question="What was revenue?",
        workflow_type="local_only",
        expected_tools=["local_search"],
        expected_sources=[],
        expected_terms=["revenue"],
    )

    def fake_query(_question):
        return {
            "output": "Revenue was available.",
            "sources": [],
            "messages": [{"type": "tool", "name": "local_search", "content": ""}],
        }

    result = evaluate_case(case, run_query=fake_query, retrieve=lambda _question: [])
    summary = summarize_results([result])

    assert summary["case_count"] == 1
    assert summary["retrieval_hit_rate"] == 1
    assert "local_only" in summary["latency_by_workflow_type"]
