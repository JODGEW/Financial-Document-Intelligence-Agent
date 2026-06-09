"""Assemble the per-answer governance report.

Combines the signals already produced elsewhere (audit id, model, retrieved
chunks, guardrail outcome, grounding result, risk result) into the §9.3 report
structure. This module only shapes data; it does not compute grounding or risk.
"""

from __future__ import annotations

import re
from typing import Any

from governance.context_policy import POLICY as CONTEXT_POLICY, AdmissionSummary

# Prompt policy id names the internal-first answer contract the agent follows.
# The context policy id is no longer a placeholder: PR2's Context Policy Manager
# makes the admission decisions, so it reads from the loaded policy.
PROMPT_POLICY_ID = "regulated_doc_agent_v1"

_EXTERNAL_MARKER = "external context"
_EXTERNAL_BULLET_RE = re.compile(r"^\s*[-*]\s*\[", re.MULTILINE)


def external_context_used(response_text: str) -> bool:
    """Return whether the answer presents an available External Context section."""
    normalized = (response_text or "").lower()
    index = normalized.find(_EXTERNAL_MARKER)
    if index == -1:
        return False

    availability_line = normalized[index:].splitlines()[0]
    if "unavailable" in availability_line or "not available" in availability_line:
        return False
    return "available" in availability_line


def _external_source_count(response_text: str) -> int:
    """Count linked bullets under the External Context section."""
    text = response_text or ""
    index = text.lower().find(_EXTERNAL_MARKER)
    if index == -1:
        return 0
    return len(_EXTERNAL_BULLET_RE.findall(text[index:]))


def _document_versions_used(retrieved_chunks: list[dict[str, Any]]) -> int:
    """Count distinct source documents in the retrieved set.

    A proxy for document versions: the parsed chunk metadata carries the source
    filename, not a version hash, so distinct documents stand in for distinct
    versions until version hashes are surfaced on retrieved chunks.
    """
    names = {
        chunk.get("source_name") or chunk.get("source")
        for chunk in retrieved_chunks
        if chunk.get("source_name") or chunk.get("source")
    }
    return len(names)


def _decision(
    guardrail_outcome: str | None,
    human_review_required: bool,
    risk_level: str,
) -> str:
    """Map governance signals to a decision outcome (§7.7).

    Precedence: a block is categorical and wins first (it already zeroes
    human_review_required, but the explicit order documents the intent). Then a
    high-risk answer is held for the Phase 5 review queue, then a medium-risk
    answer returns with a warning, then a clean answer returns.
    """
    if guardrail_outcome and guardrail_outcome.lower() == "blocked":
        return "blocked"
    if human_review_required:
        return "held_for_review"
    if risk_level == "medium":
        return "returned_with_warning"
    return "returned"


def _context_policy_section(context_admission: AdmissionSummary | None) -> dict[str, Any]:
    """Build the contextPolicy section (§7.3) from the admission summary.

    Reported honestly regardless of guardrail outcome: a blocked answer still
    retrieved chunks before the block, so the counts reflect that work even though
    the answer was not returned.
    """
    summary = context_admission or AdmissionSummary()
    return {
        "id": CONTEXT_POLICY.id,
        "selectedChunks": summary.selected_chunks,
        "droppedChunks": summary.dropped_chunks,
        "dropReasons": list(summary.drop_reasons),
        "internalTokens": summary.internal_tokens,
        "externalTokens": summary.external_tokens,
        "totalPromptTokens": summary.total_tokens,
    }


def build_report(
    audit_id: str | None,
    model: str,
    retrieved_chunks: list[dict[str, Any]],
    response_text: str,
    guardrail_outcome: str | None,
    grounding_result: dict[str, Any],
    risk_result: dict[str, Any],
    context_admission: AdmissionSummary | None = None,
) -> dict[str, Any]:
    """Build the §9.3 governance report dict for one answer."""
    chunks = retrieved_chunks or []
    grounding = grounding_result or {}
    risk = risk_result or {}

    outcome = guardrail_outcome or "passed"

    if outcome.lower() == "blocked":
        # A block is categorical, not a point on the continuous risk curve. There
        # is no answer to ground, so grounding/citation are N/A (matching how
        # eval_runner skips grounding for guardrail cases), and the risk is the
        # max. No human review: the block already happened, there is nothing for a
        # reviewer to override.
        validation = {
            "citationCoverage": None,
            "groundingScore": None,
            "unsupportedClaims": 0,
            "guardrailOutcome": outcome,
            "piiDetected": False,
        }
        risk_block = {
            "riskScore": 1.0,
            "riskLevel": "high",
            "humanReviewRequired": False,
        }
        decision = "blocked"
    else:
        human_review_required = bool(risk.get("human_review_required", False))
        risk_level = str(risk.get("risk_level", "low"))
        validation = {
            "citationCoverage": grounding.get("citation_coverage", 0.0),
            "groundingScore": grounding.get("grounding_score", 0.0),
            "unsupportedClaims": grounding.get("unsupported_claim_count", 0),
            "guardrailOutcome": outcome,
            "piiDetected": outcome.lower() == "anonymized",
        }
        risk_block = {
            "riskScore": risk.get("risk_score", 0.0),
            "riskLevel": risk_level,
            "humanReviewRequired": human_review_required,
        }
        decision = _decision(guardrail_outcome, human_review_required, risk_level)

    return {
        "auditId": audit_id,
        "model": model,
        "promptPolicyId": PROMPT_POLICY_ID,
        "contextPolicyId": CONTEXT_POLICY.id,
        "contextPolicy": _context_policy_section(context_admission),
        "sourceUsage": {
            "internalSourcesUsed": len(chunks),
            "externalSourcesUsed": _external_source_count(response_text),
            "documentVersionsUsed": _document_versions_used(chunks),
            "expiredDocumentsUsed": 0,
        },
        "validation": validation,
        "risk": risk_block,
        "decision": decision,
    }
