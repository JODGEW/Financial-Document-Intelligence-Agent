"""Context Policy Manager: decide which retrieved chunks enter the prompt.

Phase 2 of the governance layer (Governance_layer.md §7.3). A normal RAG system
asks which chunks are most relevant. A governed one also asks which are approved,
current, and within the token budget. This module loads a context policy from
YAML and admits or drops chunks against it, recording why each chunk was dropped.

It decides *what* reaches the prompt. It does not change how grounding or risk are
computed downstream; those run on whatever admit_chunks lets through.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

_POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "context_policy.yaml"

# 1 token ~= 4 characters of English text. Crude but adjustable: swap this for a
# real tokenizer if the budget math ever needs to be exact.
TOKEN_CHARS_APPROX = 4

# Drop reason strings. The report dedupes these, so keep them stable and reused.
REASON_LOW_RETRIEVAL_SCORE = "low_retrieval_score"
REASON_INTERNAL_BUDGET = "internal_context_budget_exceeded"
REASON_EXTERNAL_BUDGET = "external_context_budget_exceeded"
REASON_TOTAL_BUDGET = "total_context_budget_exceeded"
REASON_STALE = "stale_document_version"
REASON_UNAPPROVED = "unapproved_document"
REASON_POLICY_EXCLUDED = "policy_excluded"

# Chunks without a document_status read as active so the existing corpus, ingested
# before this manager existed, stays admissible.
_ACTIVE_STATUS = "active"


@dataclass
class ContextPolicy:
    """Loaded from policies/context_policy.yaml. Field set is the §7.3 example.

    Defaults match the YAML so a missing or unreadable file still runs a sane
    policy (same fallback contract as risk_scorer).
    """

    id: str = "regulated_doc_agent_v1"
    max_total_context_tokens: int = 12000
    max_internal_context_tokens: int = 10000
    max_external_context_tokens: int = 1500
    require_internal_first: bool = True
    require_chunk_metadata: bool = True
    exclude_expired_documents: bool = True
    exclude_unapproved_documents: bool = True
    allow_web_fallback: bool = True
    web_fallback_requires_local_miss: bool = True
    preserve_citation_traceability: bool = True
    # 0.0 means no score filtering. Set >0 to opt in to dropping low-score chunks.
    min_retrieval_score: float = 0.0


@dataclass
class DropDecision:
    """Why one chunk did not enter the prompt."""

    chunk_id: str
    reason: str
    detail: str


def approx_tokens(text: str) -> int:
    """Approximate token count for a string (len // TOKEN_CHARS_APPROX)."""
    return len(text or "") // TOKEN_CHARS_APPROX


def load_policy(path: str | None = None) -> ContextPolicy:
    """Load the context policy from YAML, falling back to baked-in defaults.

    Mirrors risk_scorer's fallback: a missing or unreadable file never crashes the
    agent, it just runs the default policy. Unknown YAML keys are ignored so the
    file can carry notes the code does not enforce yet.
    """
    target = Path(path) if path else _POLICY_PATH
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ContextPolicy()

    data = raw.get("context_policy") or {}
    known = {f.name for f in fields(ContextPolicy)}
    return ContextPolicy(**{key: value for key, value in data.items() if key in known})


# Loaded once at import, like risk_scorer's THRESHOLDS/WEIGHTS.
POLICY = load_policy()


def _chunk_text(chunk: dict[str, Any]) -> str:
    """Best-effort text of a chunk for token counting."""
    for key in ("content", "page_content", "text", "excerpt"):
        value = chunk.get(key)
        if value:
            return str(value)
    return ""


def _chunk_id(chunk: dict[str, Any], index: int) -> str:
    """A stable-ish identifier for a chunk, for drop-decision records."""
    for key in ("chunk_id", "id"):
        if chunk.get(key):
            return str(chunk[key])
    source = chunk.get("source_name") or chunk.get("source") or "chunk"
    page = chunk.get("page")
    page_part = f"#p{page}" if page is not None else ""
    return f"{source}{page_part}#{index}"


def _document_status(chunk: dict[str, Any]) -> str:
    """Return the chunk's document status (top-level or nested), default active."""
    status = chunk.get("document_status")
    if status is None:
        metadata = chunk.get("metadata") or {}
        status = metadata.get("document_status")
    return str(status or _ACTIVE_STATUS).lower()


def admit_chunks(
    chunks: list[dict[str, Any]],
    policy: ContextPolicy,
    *,
    is_external: bool,
) -> tuple[list[dict[str, Any]], list[DropDecision]]:
    """Select chunks that pass policy. Return (selected, drop_decisions).

    Checks run per chunk in this order: document validity (expired, then draft),
    retrieval score, then token budgets accumulated in retrieval-rank order. A
    chunk that overflows a budget is dropped but later, smaller chunks still get a
    chance to fit (greedy fill).

    The total-token budget is enforced within this single call only. Internal and
    external chunks arrive on separate calls (local_search vs web_search), so the
    cross-call total is not enforced here; see the GOVERNANCE_PROGRESS Decisions
    log. With the default caps (10000 + 1500 < 12000) the total never binds.
    """
    selected: list[dict[str, Any]] = []
    drops: list[DropDecision] = []

    side_tokens = 0  # running internal-or-external total for this call
    call_tokens = 0  # running total for this call (for max_total)

    if is_external:
        side_budget = policy.max_external_context_tokens
        side_reason = REASON_EXTERNAL_BUDGET
        side_budget_name = "max_external_context_tokens"
    else:
        side_budget = policy.max_internal_context_tokens
        side_reason = REASON_INTERNAL_BUDGET
        side_budget_name = "max_internal_context_tokens"

    for index, chunk in enumerate(chunks):
        chunk_id = _chunk_id(chunk, index)
        status = _document_status(chunk)

        if policy.exclude_expired_documents and status == "expired":
            drops.append(DropDecision(chunk_id, REASON_STALE, "document_status=expired"))
            continue
        if policy.exclude_unapproved_documents and status == "draft":
            drops.append(DropDecision(chunk_id, REASON_UNAPPROVED, "document_status=draft"))
            continue

        score = chunk.get("score")
        if policy.min_retrieval_score > 0 and score is not None and score < policy.min_retrieval_score:
            drops.append(
                DropDecision(
                    chunk_id,
                    REASON_LOW_RETRIEVAL_SCORE,
                    f"score={score} < min_retrieval_score={policy.min_retrieval_score}",
                )
            )
            continue

        tokens = approx_tokens(_chunk_text(chunk))

        if side_tokens + tokens > side_budget:
            drops.append(
                DropDecision(
                    chunk_id,
                    side_reason,
                    f"{side_tokens + tokens} tokens > {side_budget_name}={side_budget}",
                )
            )
            continue
        if call_tokens + tokens > policy.max_total_context_tokens:
            drops.append(
                DropDecision(
                    chunk_id,
                    REASON_TOTAL_BUDGET,
                    f"{call_tokens + tokens} tokens > max_total_context_tokens={policy.max_total_context_tokens}",
                )
            )
            continue

        side_tokens += tokens
        call_tokens += tokens
        selected.append(chunk)

    return selected, drops


# --- Admission stash ---------------------------------------------------------
#
# Drop decisions reach the governance report on an explicit AdmissionSummary that
# the agent creates per query and passes into the graph run config
# (`configurable.admission_stash`). The tools receive it through LangGraph's
# injected RunnableConfig and fold their results into it.
#
# Why a config-threaded object and not ambient state: local_search/web_search are
# @tool functions whose string return value is consumed by the agent, so their
# drop decisions cannot ride that string. An earlier ContextVar version worked for
# the synchronous query() path but silently broke under streaming, where Starlette
# drives the response generator across threadpool contexts and a ContextVar set in
# the generator does not survive to the tool's executor thread. A plain object
# passed by reference through the run config is shared regardless of threading, so
# it is correct for both /api/chat and /api/chat/stream. See the Decisions log.

@dataclass
class AdmissionSummary:
    """Aggregated admission outcome for one query, read by the report builder."""

    selected_chunks: int = 0
    dropped_chunks: int = 0
    drop_reasons: list[str] = field(default_factory=list)
    internal_tokens: int = 0
    external_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.internal_tokens + self.external_tokens

    def record(
        self,
        selected: list[dict[str, Any]],
        drops: list[DropDecision],
        *,
        is_external: bool,
    ) -> None:
        """Fold one admit_chunks result into this summary."""
        self.selected_chunks += len(selected)
        self.dropped_chunks += len(drops)
        tokens = sum(approx_tokens(_chunk_text(chunk)) for chunk in selected)
        if is_external:
            self.external_tokens += tokens
        else:
            self.internal_tokens += tokens
        for drop in drops:
            if drop.reason not in self.drop_reasons:
                self.drop_reasons.append(drop.reason)
