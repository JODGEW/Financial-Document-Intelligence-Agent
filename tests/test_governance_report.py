"""Tests for the governance report builder."""

import json

from governance.governance_report import build_report, external_context_used

_CHUNKS = [
    {"source_name": "policy.md", "source": "docs/policy.md", "page": None, "excerpt": "a"},
    {"source_name": "policy.md", "source": "docs/policy.md", "page": 2, "excerpt": "b"},
    {"source_name": "acme-10k.pdf", "source": "docs/acme-10k.pdf", "page": 4, "excerpt": "c"},
]
_GROUNDING = {
    "citation_coverage": 0.9,
    "grounding_score": 0.85,
    "unsupported_claims": ["42%"],
    "unsupported_claim_count": 1,
}
_RISK = {
    "risk_score": 0.32,
    "risk_level": "low",
    "risk_reasons": [],
    "human_review_required": False,
}


def test_report_matches_section_structure_and_is_json_serializable():
    """The report carries the §9.3 camelCase structure and serializes to JSON."""
    report = build_report(
        audit_id="audit-1",
        model="claude-haiku-4.5",
        retrieved_chunks=_CHUNKS,
        response_text="Answer [policy.md].",
        guardrail_outcome=None,
        grounding_result=_GROUNDING,
        risk_result=_RISK,
    )

    # Round-trips through JSON without error.
    assert json.loads(json.dumps(report)) == report

    assert set(report) == {
        "auditId", "model", "promptPolicyId", "contextPolicyId",
        "contextPolicy", "sourceUsage", "validation", "risk", "decision",
    }
    assert set(report["sourceUsage"]) == {
        "internalSourcesUsed", "externalSourcesUsed",
        "documentVersionsUsed", "expiredDocumentsUsed",
    }
    assert set(report["validation"]) == {
        "citationCoverage", "groundingScore", "unsupportedClaims",
        "guardrailOutcome", "piiDetected",
    }
    assert set(report["risk"]) == {"riskScore", "riskLevel", "humanReviewRequired"}
    assert report["validation"]["citationCoverage"] == 0.9
    assert report["validation"]["unsupportedClaims"] == 1


def test_source_usage_counts_documents_and_external_bullets():
    """Internal count is chunk count; versions are distinct docs; external is bullets."""
    response = (
        "## Result Summary\n\n"
        "Internal Corpus Answer: unavailable in current local corpus.\n\n"
        "External Context: Available.\n\n"
        "- [sec.gov](https://sec.gov/a) | 2025 | Filing A\n"
        "- [reuters.com](https://reuters.com/b) | 2025 | Headline B\n"
    )
    report = build_report(
        audit_id="audit-2",
        model="m",
        retrieved_chunks=_CHUNKS,
        response_text=response,
        guardrail_outcome=None,
        grounding_result=_GROUNDING,
        risk_result=_RISK,
    )
    assert report["sourceUsage"]["internalSourcesUsed"] == 3
    assert report["sourceUsage"]["documentVersionsUsed"] == 2  # policy.md + acme-10k.pdf
    assert report["sourceUsage"]["externalSourcesUsed"] == 2
    assert external_context_used(response) is True


def test_decision_routing_covers_each_outcome():
    """Decision maps from guardrail, review, and risk level signals (§7.7)."""
    base = dict(
        audit_id="a", model="m", retrieved_chunks=_CHUNKS,
        response_text="x", grounding_result=_GROUNDING,
    )

    blocked = build_report(**base, guardrail_outcome="blocked", risk_result=_RISK)
    assert blocked["decision"] == "blocked"

    review = build_report(
        **base, guardrail_outcome=None,
        risk_result={**_RISK, "risk_level": "high", "human_review_required": True},
    )
    assert review["decision"] == "requires_review"

    warning = build_report(
        **base, guardrail_outcome=None,
        risk_result={**_RISK, "risk_level": "medium", "human_review_required": False},
    )
    assert warning["decision"] == "returned_with_warning"

    returned = build_report(**base, guardrail_outcome=None, risk_result=_RISK)
    assert returned["decision"] == "returned"


def test_blocked_answer_is_scored_categorically():
    """A block reads High risk with N/A grounding, regardless of the formula.

    The continuous risk formula on a refusal message would land in Medium (~0.55),
    which reads as broken next to a Blocked decision. A block is categorical: max
    risk, no answer to ground, and no review (the block already happened).
    """
    from governance.context_policy import AdmissionSummary, DropDecision

    # Chunks were retrieved before the block. The contextPolicy section reports
    # that honestly even though grounding is N/A.
    admission = AdmissionSummary()
    admission.record(
        selected=[{"content": "x" * 40}],
        drops=[DropDecision("c1", "stale_document_version", "expired")],
        is_external=False,
    )

    report = build_report(
        audit_id="a",
        model="m",
        retrieved_chunks=[],
        response_text="This request was blocked by the ReAct-RAG safety policy.",
        guardrail_outcome="blocked",
        # Inputs that the continuous path would score as Medium 0.55 / 50% grounding.
        grounding_result={"citation_coverage": 0.0, "grounding_score": 0.5, "unsupported_claim_count": 0},
        risk_result={"risk_score": 0.55, "risk_level": "medium", "human_review_required": False},
        context_admission=admission,
    )

    assert report["validation"]["citationCoverage"] is None
    assert report["validation"]["groundingScore"] is None
    assert report["validation"]["unsupportedClaims"] == 0
    assert report["risk"]["riskScore"] == 1.0
    assert report["risk"]["riskLevel"] == "high"
    assert report["risk"]["humanReviewRequired"] is False
    assert report["decision"] == "blocked"
    # contextPolicy is not N/A on a block: it reflects pre-block retrieval.
    assert report["contextPolicy"]["selectedChunks"] == 1
    assert report["contextPolicy"]["droppedChunks"] == 1
    assert report["contextPolicy"]["dropReasons"] == ["stale_document_version"]
    # Still JSON-serializable with the null grounding fields.
    import json
    assert json.loads(json.dumps(report))["validation"]["groundingScore"] is None


def test_context_policy_section_populated_and_dedupes_reasons():
    """contextPolicy carries the §7.3 fields; drop reasons dedupe across tool calls."""
    from governance.context_policy import AdmissionSummary, DropDecision

    admission = AdmissionSummary()
    admission.record(
        selected=[{"content": "a" * 40}],  # 10 internal tokens
        drops=[
            DropDecision("c1", "stale_document_version", "expired"),
            DropDecision("c2", "low_retrieval_score", "below threshold"),
        ],
        is_external=False,
    )
    admission.record(
        selected=[{"content": "b" * 80}],  # 20 external tokens
        drops=[DropDecision("c3", "stale_document_version", "expired")],  # repeat reason
        is_external=True,
    )

    report = build_report(
        audit_id="a",
        model="m",
        retrieved_chunks=_CHUNKS,
        response_text="Answer [policy.md].",
        guardrail_outcome=None,
        grounding_result=_GROUNDING,
        risk_result=_RISK,
        context_admission=admission,
    )

    context_policy = report["contextPolicy"]
    assert set(context_policy) == {
        "id", "selectedChunks", "droppedChunks", "dropReasons",
        "internalTokens", "externalTokens", "totalPromptTokens",
    }
    assert context_policy["id"] == "regulated_doc_agent_v1"
    assert context_policy["selectedChunks"] == 2
    assert context_policy["droppedChunks"] == 3
    # Deduped, order preserved across both calls.
    assert context_policy["dropReasons"] == ["stale_document_version", "low_retrieval_score"]
    assert context_policy["internalTokens"] == 10
    assert context_policy["externalTokens"] == 20
    assert context_policy["totalPromptTokens"] == 30
    # Still JSON-serializable with the new section.
    assert json.loads(json.dumps(report)) == report


def test_missing_inputs_default_gracefully():
    """No guardrail outcome reads as passed; empty results fall back to defaults."""
    report = build_report(
        audit_id=None,
        model="m",
        retrieved_chunks=[],
        response_text="",
        guardrail_outcome=None,
        grounding_result={},
        risk_result={},
    )
    assert report["validation"]["guardrailOutcome"] == "passed"
    assert report["validation"]["piiDetected"] is False
    assert report["validation"]["citationCoverage"] == 0.0
    assert report["sourceUsage"]["internalSourcesUsed"] == 0
    assert report["risk"]["riskLevel"] == "low"
    assert report["decision"] == "returned"
    # anonymized guardrail surfaces PII detection.
    anonymized = build_report(
        audit_id=None, model="m", retrieved_chunks=[], response_text="",
        guardrail_outcome="anonymized", grounding_result={}, risk_result={},
    )
    assert anonymized["validation"]["piiDetected"] is True
