"""Consistency check: runtime grounding validator vs eval_runner.

Runs the agent on each scored eval case and compares the runtime validator's
citation coverage and unsupported-claim rate against eval_runner's, on identical
(answer, retrieved-sources) inputs. Prints any case where they disagree beyond
rounding. An empty diff means the two implementations agree.

    python scripts/check_grounding_consistency.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import query
from eval_runner import (
    citation_accuracy,
    evidence_text,
    extract_claim_tokens,
    load_eval_cases,
    retrieved_docs_to_sources,
    unsupported_claim_rate,
)
from governance.grounding_validator import validate
from tools import get_retriever


def main() -> int:
    cases = load_eval_cases()
    retriever = get_retriever()

    diffs: list[str] = []
    compared = 0

    for case in cases:
        # eval_runner only scores grounding on local cases; guardrail-block and
        # missing-company-fallback cases compute citation/claim metrics as None.
        if case.expected_guardrail_outcome is not None:
            continue
        if case.workflow_type == "missing_company_fallback":
            continue

        docs = retriever.invoke(case.question)
        retrieved_sources = retrieved_docs_to_sources(docs)
        result = query(case.question)
        answer = str(result.get("output", ""))
        answer_sources = result.get("sources") or retrieved_sources

        eval_cite = round(citation_accuracy(answer, answer_sources), 4)
        eval_unsupported = round(
            unsupported_claim_rate(answer, evidence_text(answer_sources)), 4
        )

        runtime = validate(answer, answer_sources)
        claims = extract_claim_tokens(answer)
        runtime_unsupported = round(
            runtime["unsupported_claim_count"] / len(claims) if claims else 0.0, 4
        )

        compared += 1
        if runtime["citation_coverage"] != eval_cite:
            diffs.append(
                f"  {case.id}: citation_coverage runtime={runtime['citation_coverage']} "
                f"vs eval={eval_cite}"
            )
        if runtime_unsupported != eval_unsupported:
            diffs.append(
                f"  {case.id}: unsupported_rate runtime={runtime_unsupported} "
                f"vs eval={eval_unsupported}"
            )

    print(f"Compared {compared} scored eval cases.")
    if diffs:
        print("Diffs (runtime grounding disagrees with eval_runner):")
        for line in diffs:
            print(line)
        return 1
    print("No diffs. Runtime grounding matches eval_runner within rounding.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
