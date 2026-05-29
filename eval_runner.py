"""Evaluation runner for retrieval, grounding, citations, routing, and latency."""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from agent import query
from tools import get_retriever


DEFAULT_EVAL_PATH = Path(__file__).resolve().parent / "eval" / "questions.jsonl"
CITATION_RE = re.compile(
    r"(?P<source>[\w.-]+\.(?:pdf|md|txt))(?:[^.\n]{0,80}?\bpage\s+(?P<page>\d+))?",
    re.IGNORECASE,
)
CLAIM_RE = re.compile(
    r"\$?\b\d+(?:\.\d+)?\s*(?:%|million|billion|days?|hours?|quarters?|years?)?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExpectedSource:
    """Expected retrieved source metadata for one eval case."""

    source_name: str
    page: int | None = None


@dataclass(frozen=True)
class EvalCase:
    """One evaluation question and its expected behavior."""

    id: str
    question: str
    workflow_type: str
    expected_tools: list[str]
    expected_sources: list[ExpectedSource]
    expected_terms: list[str]
    expected_guardrail_outcome: str | None = None


@dataclass(frozen=True)
class Citation:
    """A cited source parsed from an answer."""

    source_name: str
    page: int | None = None


@dataclass(frozen=True)
class EvalResult:
    """Metric result for one evaluation case."""

    case_id: str
    workflow_type: str
    retrieval_hit: bool
    grounded_answer: bool
    unsupported_claim_rate: float | None
    citation_accuracy: float | None
    tool_routing_hit: bool
    local_refusal_correct: bool | None
    guardrail_outcome: str | None
    guardrail_outcome_correct: bool | None
    latency_seconds: float


def _require_str(row: dict[str, Any], key: str, line_number: int) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Line {line_number}: {key} must be a non-empty string.")
    return value


def _require_str_list(row: dict[str, Any], key: str, line_number: int) -> list[str]:
    value = row.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"Line {line_number}: {key} must be a list of strings.")
    return value


def _load_expected_sources(row: dict[str, Any], line_number: int) -> list[ExpectedSource]:
    value = row.get("expected_sources")
    if not isinstance(value, list):
        raise ValueError(f"Line {line_number}: expected_sources must be a list.")

    sources = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"Line {line_number}: expected_sources entries must be objects.")
        source_name = item.get("source_name")
        if not isinstance(source_name, str) or not source_name:
            raise ValueError(f"Line {line_number}: expected source_name must be a string.")
        page = item.get("page")
        if page is not None and not isinstance(page, int):
            raise ValueError(f"Line {line_number}: expected source page must be an integer.")
        sources.append(ExpectedSource(source_name=source_name, page=page))
    return sources


def load_eval_cases(path: str | Path = DEFAULT_EVAL_PATH) -> list[EvalCase]:
    """Load and validate JSONL evaluation cases."""
    eval_path = Path(path)
    cases = []
    with eval_path.open(encoding="utf-8") as eval_file:
        for line_number, raw_line in enumerate(eval_file, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Line {line_number}: invalid JSON.") from exc
            if not isinstance(row, dict):
                raise ValueError(f"Line {line_number}: each row must be a JSON object.")

            expected_guardrail = row.get("expected_guardrail_outcome")
            if expected_guardrail is not None and not isinstance(expected_guardrail, str):
                raise ValueError(
                    f"Line {line_number}: expected_guardrail_outcome must be a string when set."
                )
            cases.append(
                EvalCase(
                    id=_require_str(row, "id", line_number),
                    question=_require_str(row, "question", line_number),
                    workflow_type=_require_str(row, "workflow_type", line_number),
                    expected_tools=row.get("expected_tools", []) if expected_guardrail else _require_str_list(row, "expected_tools", line_number),
                    expected_sources=_load_expected_sources(row, line_number),
                    expected_terms=row.get("expected_terms", []) if expected_guardrail else _require_str_list(row, "expected_terms", line_number),
                    expected_guardrail_outcome=expected_guardrail,
                )
            )

    if not cases:
        raise ValueError("Evaluation dataset is empty.")
    return cases


def source_name(source: str | None) -> str:
    """Normalize source metadata to a filename for comparisons."""
    return Path(source or "").name


def source_matches(expected: ExpectedSource, actual: dict[str, Any]) -> bool:
    """Return whether a retrieved source matches expected file/page metadata."""
    actual_name = actual.get("source_name") or source_name(actual.get("source"))
    if actual_name != expected.source_name:
        return False
    if expected.page is None:
        return True
    return actual.get("page") == expected.page


def retrieval_hit(expected_sources: list[ExpectedSource], retrieved_sources: list[dict[str, Any]]) -> bool:
    """Return whether all expected sources appear in retrieved metadata."""
    if not expected_sources:
        return True
    return all(
        any(source_matches(expected, actual) for actual in retrieved_sources)
        for expected in expected_sources
    )


def parse_citations(answer: str) -> list[Citation]:
    """Parse simple filename and optional page citations from answer text."""
    citations = []
    seen = set()
    for match in CITATION_RE.finditer(answer):
        page = match.group("page")
        citation = Citation(
            source_name=match.group("source"),
            page=int(page) if page is not None else None,
        )
        key = (citation.source_name, citation.page)
        if key in seen:
            continue
        seen.add(key)
        citations.append(citation)
    return citations


def citation_accuracy(answer: str, retrieved_sources: list[dict[str, Any]]) -> float:
    """Return the share of parsed citations that match retrieved metadata."""
    citations = parse_citations(answer)
    if not citations:
        return 1.0 if not retrieved_sources else 0.0

    matched = 0
    for citation in citations:
        expected = ExpectedSource(citation.source_name, citation.page)
        if any(source_matches(expected, actual) for actual in retrieved_sources):
            matched += 1
    return matched / len(citations)


def extract_claim_tokens(answer: str) -> list[str]:
    """Extract simple numeric claims for rule-based support checks."""
    tokens = []
    seen = set()
    for match in CLAIM_RE.finditer(answer):
        token = " ".join(match.group(0).lower().split())
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def unsupported_claim_rate(answer: str, evidence_text: str) -> float:
    """Return the share of numeric claims absent from retrieved evidence."""
    claims = extract_claim_tokens(answer)
    if not claims:
        return 0.0

    normalized_evidence = evidence_text.lower()
    unsupported = sum(1 for claim in claims if claim not in normalized_evidence)
    return unsupported / len(claims)


def grounded_answer(answer: str, expected_terms: list[str], evidence_text: str) -> bool:
    """Rule-based groundedness check for expected terms and numeric claims."""
    normalized_answer = answer.lower()
    has_expected_terms = all(term.lower() in normalized_answer for term in expected_terms)
    return has_expected_terms and unsupported_claim_rate(answer, evidence_text) == 0.0


def extract_tool_names(messages: list[Any]) -> list[str]:
    """Extract tool names from LangChain or synthetic trace messages."""
    tool_names = []
    call_name_by_id = {}
    for message in messages:
        calls = []
        if isinstance(message, dict):
            calls = message.get("tool_calls", [])
        else:
            calls = getattr(message, "tool_calls", None) or []
        for call in calls:
            if isinstance(call, dict) and call.get("id") and call.get("name"):
                call_name_by_id[call["id"]] = call["name"]

    for message in messages:
        name = None
        tool_call_id = None
        if isinstance(message, dict):
            name = message.get("name")
            tool_call_id = message.get("tool_call_id")
        else:
            name = getattr(message, "name", None)
            tool_call_id = getattr(message, "tool_call_id", None)
        if not name and tool_call_id:
            name = call_name_by_id.get(tool_call_id)
        if name and name not in tool_names:
            tool_names.append(name)
    return tool_names


def tool_routing_hit(expected_tools: list[str], messages: list[Any]) -> bool:
    """Return whether expected tools were used at least once."""
    used_tools = extract_tool_names(messages)
    return all(tool in used_tools for tool in expected_tools)


def retrieved_docs_to_sources(docs: list[Any]) -> list[dict[str, Any]]:
    """Convert LangChain Documents into source metadata dictionaries."""
    sources = []
    for rank, doc in enumerate(docs, 1):
        metadata = getattr(doc, "metadata", {}) or {}
        source = metadata.get("source", "unknown")
        sources.append(
            {
                "rank": rank,
                "source": source,
                "source_name": source_name(source),
                "page": metadata.get("page"),
                "excerpt": getattr(doc, "page_content", ""),
            }
        )
    return sources


def evidence_text(retrieved_sources: list[dict[str, Any]]) -> str:
    """Join retrieved excerpts for claim-support checks."""
    return "\n".join(str(source.get("excerpt", "")) for source in retrieved_sources)


REFUSAL_MARKERS = (
    "not in local corpus",
    "not in the local corpus",
    "not present in the local corpus",
    "internal corpus answer:</span> unavailable",
    "internal corpus answer: unavailable",
)


def local_refusal_correct(answer: str) -> bool:
    """Return whether the Internal Corpus Answer admits the topic is missing."""
    normalized = answer.lower()
    return any(marker in normalized for marker in REFUSAL_MARKERS)


def evaluate_case(
    case: EvalCase,
    run_query: Callable[[str], dict[str, Any]] = query,
    retrieve: Callable[[str], list[Any]] | None = None,
) -> EvalResult:
    """Run one eval case and compute metrics."""
    retriever = retrieve
    if retriever is None:
        live_retriever = get_retriever()
        retriever = live_retriever.invoke

    started = time.perf_counter()
    docs = retriever(case.question)
    retrieved_sources = retrieved_docs_to_sources(docs)
    agent_result = run_query(case.question)
    latency_seconds = time.perf_counter() - started

    answer = str(agent_result.get("output", ""))
    messages = agent_result.get("messages", [])
    answer_sources = agent_result.get("sources") or retrieved_sources
    text_for_support = evidence_text(answer_sources)

    is_fallback = case.workflow_type == "missing_company_fallback"
    is_guardrail_case = case.expected_guardrail_outcome is not None
    actual_guardrail = agent_result.get("guardrail_outcome")

    if is_guardrail_case:
        # Guardrail-block cases: only the safety outcome matters. Don't score
        # local grounding/citations/refusal; the agent is supposed to refuse.
        unsupported_rate: float | None = None
        cite_accuracy: float | None = None
        refusal_correct: bool | None = None
        guardrail_correct: bool | None = actual_guardrail == case.expected_guardrail_outcome
        grounded = bool(guardrail_correct)
    elif is_fallback:
        # Local retrieval is a distractor for fallback cases — scoring claims
        # against it is meaningless. We measure refusal correctness instead.
        unsupported_rate = None
        cite_accuracy = None
        refusal_correct = local_refusal_correct(answer)
        guardrail_correct = None
        grounded = bool(refusal_correct) and all(
            term.lower() in answer.lower() for term in case.expected_terms
        )
    else:
        unsupported_rate = unsupported_claim_rate(answer, text_for_support)
        cite_accuracy = citation_accuracy(answer, answer_sources)
        refusal_correct = None
        guardrail_correct = None
        grounded = grounded_answer(answer, case.expected_terms, text_for_support)

    return EvalResult(
        case_id=case.id,
        workflow_type=case.workflow_type,
        retrieval_hit=retrieval_hit(case.expected_sources, retrieved_sources),
        grounded_answer=grounded,
        unsupported_claim_rate=unsupported_rate,
        citation_accuracy=cite_accuracy,
        tool_routing_hit=tool_routing_hit(case.expected_tools, messages),
        local_refusal_correct=refusal_correct,
        guardrail_outcome=actual_guardrail,
        guardrail_outcome_correct=guardrail_correct,
        latency_seconds=latency_seconds,
    )


def summarize_results(results: list[EvalResult]) -> dict[str, Any]:
    """Aggregate eval results into report-ready metrics."""
    if not results:
        raise ValueError("Cannot summarize an empty result set.")

    latency_by_workflow = {}
    for workflow_type in sorted({result.workflow_type for result in results}):
        latencies = [
            result.latency_seconds
            for result in results
            if result.workflow_type == workflow_type
        ]
        latency_by_workflow[workflow_type] = mean(latencies)

    grounded_scored = [r.unsupported_claim_rate for r in results if r.unsupported_claim_rate is not None]
    cite_scored = [r.citation_accuracy for r in results if r.citation_accuracy is not None]
    refusal_scored = [r.local_refusal_correct for r in results if r.local_refusal_correct is not None]
    guardrail_scored = [r.guardrail_outcome_correct for r in results if r.guardrail_outcome_correct is not None]

    return {
        "case_count": len(results),
        "retrieval_hit_rate": mean(result.retrieval_hit for result in results),
        "grounded_answer_rate": mean(result.grounded_answer for result in results),
        "unsupported_claim_rate": mean(grounded_scored) if grounded_scored else None,
        "citation_accuracy": mean(cite_scored) if cite_scored else None,
        "tool_routing_accuracy": mean(result.tool_routing_hit for result in results),
        "local_refusal_accuracy": mean(refusal_scored) if refusal_scored else None,
        "guardrail_block_accuracy": mean(guardrail_scored) if guardrail_scored else None,
        "latency_by_workflow_type": latency_by_workflow,
    }


def run_evaluation(path: str | Path = DEFAULT_EVAL_PATH) -> dict[str, Any]:
    """Run all eval cases and return detailed and aggregate metrics."""
    cases = load_eval_cases(path)
    results = [evaluate_case(case) for case in cases]
    return {
        "summary": summarize_results(results),
        "results": [result.__dict__ for result in results],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run RAG evaluation metrics.")
    parser.add_argument("--dataset", default=str(DEFAULT_EVAL_PATH))
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args()

    report = run_evaluation(args.dataset)
    if args.json:
        print(json.dumps(report, indent=2))
        return

    summary = report["summary"]

    def fmt_pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.2%}"

    print("Evaluation summary")
    print(f"Cases: {summary['case_count']}")
    print(f"Retrieval hit rate: {fmt_pct(summary['retrieval_hit_rate'])}")
    print(f"Grounded answer rate: {fmt_pct(summary['grounded_answer_rate'])}")
    print(f"Unsupported claim rate (local): {fmt_pct(summary['unsupported_claim_rate'])}")
    print(f"Citation accuracy (local): {fmt_pct(summary['citation_accuracy'])}")
    print(f"Tool-routing accuracy: {fmt_pct(summary['tool_routing_accuracy'])}")
    print(f"Local-refusal accuracy (fallback): {fmt_pct(summary['local_refusal_accuracy'])}")
    print(f"Guardrail-block accuracy: {fmt_pct(summary['guardrail_block_accuracy'])}")
    print("Latency by workflow type:")
    for workflow_type, latency in summary["latency_by_workflow_type"].items():
        print(f"  {workflow_type}: {latency:.2f}s")


if __name__ == "__main__":
    main()
