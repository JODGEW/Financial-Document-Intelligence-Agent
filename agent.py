"""Agent setup using LangChain create_agent and AWS Bedrock."""

from collections.abc import Iterator
from datetime import datetime, timezone
import uuid
import warnings

from langchain.agents import create_agent
from langchain_aws import ChatBedrock

from audit import SOURCE_HEADER_RE, build_audit_record, extract_retrieved_sources, write_audit_record
import config
from governance import review_queue
from governance.context_policy import AdmissionSummary
from governance.governance_report import build_report, external_context_used
from governance.grounding_validator import validate as validate_grounding
from governance.risk_scorer import score as score_risk
from tools import ALL_TOOLS, web_search


GUARDRAIL_INTERVENTION_MARKERS = (
    "blocked by the react-rag safety policy",
    "this request was blocked by",
    "the model's response was blocked by",
)

SYSTEM_PROMPT = """\
You are a financial document intelligence agent. Your primary job is to answer
questions using internal documents stored in the local corpus.

Rules:
1. Always search the local document corpus FIRST using the local_search tool.
   If the user asks a question without naming a specific company (e.g. "What
   are the risk factors?"), search the corpus anyway and infer the company
   from the retrieved documents. Do not ask the user to clarify which company
   when the corpus contains documents from only one company.
2. Only use web_search when the local corpus clearly does not contain the
   needed information.
3. When using information from local documents, cite the source as a bracketed
   tag at the END of each fact-bearing sentence using the EXACT filename:
   [acme-corp-10k-excerpt-2025.pdf p.2] or [compliance-policy-personal-trading.md].
   Do not write prose source notes like "Source: Acme Corporation Form 10-K…";
   the bracketed tag is the only acceptable citation form. Every numeric or
   factual claim must carry one. If a sentence draws on multiple sources, list
   each tag.
4. When using web results, clearly label them as EXTERNAL CONTEXT and separate
   them from local document evidence.
5. If you cannot find enough information to answer confidently, say so rather
   than guessing.
6. Be concise and precise. Ground every claim in retrieved evidence. Quote
   numbers in the exact form they appear in the source. Do NOT derive,
   compute, or convert numbers (e.g. if source says "Twenty-one companies
   (50%)", do not write "21 of 42"). If a number is not stated literally in
   the source, do not state it.
7. If the requested company or topic is not present in the local corpus, state
   that directly in the Internal Corpus Answer. Do not answer the internal
   corpus section using a different company's data.
8. If web results are available, present them as separately labeled external
   context with specific sources, dates, and headlines when possible. If that
   information is not available from the retrieved results, say so explicitly.
9. If web results are ambiguous, low-quality, or do not contain enough detail,
   say that directly and do not summarize them as verified facts.
10. If the requested filings are not in the local corpus, do not paraphrase
    filing content from weak web snippets as if it were fully verified.
11. For web results, preserve the EXACT markdown format returned by the tool.
    Each result is a markdown bullet with a clickable link. Pass them through
    as-is under the External Context section. Do not rewrite or reformat them.
12. If the requested company is not present in the local corpus, do not describe
    the contents of a different company's filings in the Internal Corpus Answer.
    You MUST use web_search for separately labeled External Context when the
    user asks about public filings, current public information, latest filings,
    SEC filings, 10-K/10-Q content, or another named company that is missing
    from the local corpus. Do not ask the user to request web search separately.
13. Do not add notes, caveats, or explanatory paragraphs after the Result
    Summary sections. All limitations (e.g. "dates unavailable", "limited
    detail") must be folded into the availability line itself (e.g.
    "partially available — dates and detail limited").

Output format — use EXACTLY this structure. The answer starts at "## Result
Summary" and ends at the last bullet under External Context. NOTHING comes
after the last bullet — no notes, no caveats, no recommendations, no
trailing sentences. Use the HTML color tags EXACTLY as shown below.

    ## Result Summary

    <span style="color: #2e7d32; font-weight: bold;">Internal Corpus Answer:</span> [available / unavailable in current local corpus].
    [If available: the substantive answer from local documents with citations.]
    [If unavailable: one sentence — just the company name and "not in local corpus".]

    <span style="color: #1565c0; font-style: italic;">External Context:</span> [available / partially available / unavailable].
    [Fold any quality caveats into this line, e.g. "partially available — dates
    and detail limited".]

    [If available: structured web result bullets. STOP after the last bullet.]

    [If available: structured web results as bullets, each with
    Source | Date | Headline | Summary on one line.]
"""


_warned_no_guardrail = False


def _guardrail_kwargs() -> dict:
    """Return ChatBedrock kwargs for guardrails, or {} when unconfigured."""
    global _warned_no_guardrail
    if not (config.BEDROCK_GUARDRAIL_ID and config.BEDROCK_GUARDRAIL_VERSION):
        if not _warned_no_guardrail:
            warnings.warn(
                "Bedrock guardrails are not configured "
                "(BEDROCK_GUARDRAIL_ID / BEDROCK_GUARDRAIL_VERSION). "
                "Run scripts/setup_guardrail.py to provision; see "
                "policies/guardrails-policy.md.",
                stacklevel=2,
            )
            _warned_no_guardrail = True
        return {}
    return {
        "guardrails": {
            "guardrailIdentifier": config.BEDROCK_GUARDRAIL_ID,
            "guardrailVersion": config.BEDROCK_GUARDRAIL_VERSION,
            "trace": config.BEDROCK_GUARDRAIL_TRACE,
        }
    }


def build_agent(streaming: bool = False):
    """Create and return the agent graph."""
    llm = ChatBedrock(
        model_id=config.CHAT_MODEL_ID,
        region_name=config.AWS_REGION,
        model_kwargs={"temperature": 0},
        streaming=streaming,
        **_guardrail_kwargs(),
    )

    graph = create_agent(
        model=llm,
        tools=ALL_TOOLS,
        system_prompt=SYSTEM_PROMPT,
        debug=False,
    )
    return graph


def detect_guardrail_intervention(answer: str, messages: list) -> str | None:
    """Return a label when the response was blocked by guardrails, else None.

    Bedrock's guardrail intervention surfaces in two places:
    - The final assistant text matches one of the configured block messages.
    - One of the assistant messages carries a `stop_reason` of
      ``guardrail_intervened`` in its response_metadata.
    """
    normalized = (answer or "").lower()
    if any(marker in normalized for marker in GUARDRAIL_INTERVENTION_MARKERS):
        return "blocked"

    for message in messages:
        metadata = getattr(message, "response_metadata", None)
        if isinstance(metadata, dict):
            stop_reason = str(metadata.get("stop_reason", "")).lower()
            if "guardrail" in stop_reason:
                return "blocked"
    return None


def should_expose_retrieved_sources(output: str) -> bool:
    """Return whether retrieved chunks should be shown as answer evidence."""
    normalized = output.lower()
    marker_index = normalized.find("internal corpus answer")
    if marker_index == -1:
        return True

    availability_line = normalized[marker_index:].splitlines()[0]
    return not any(
        unavailable_marker in availability_line
        for unavailable_marker in (
            "unavailable",
            "not available",
            "not in local corpus",
            "not present in the local corpus",
        )
    )


def should_run_external_fallback(question: str, output: str) -> bool:
    """Return whether to run deterministic web fallback after agent output."""
    normalized_question = question.lower()
    normalized_output = output.lower()
    public_info_terms = (
        "latest",
        "filing",
        "filings",
        "10-k",
        "10-q",
        "sec",
        "annual report",
        "risk factor",
        "risk factors",
        "public",
    )
    internal_unavailable = "internal corpus answer" in normalized_output and any(
        marker in normalized_output
        for marker in (
            "internal corpus answer:</span> unavailable",
            "internal corpus answer: unavailable",
            "not in the local corpus",
            "not in local corpus",
        )
    )
    external_missing = (
        "external context" in normalized_output
        and "unavailable" in normalized_output
        and (
            "web search not performed" in normalized_output
            or "request a web search" in normalized_output
            or "provide access" in normalized_output
        )
    )
    return (
        internal_unavailable
        and external_missing
        and any(term in normalized_question for term in public_info_terms)
    )


def apply_external_fallback(output: str, web_results: str) -> str:
    """Replace an unavailable External Context section with web results."""
    if "not available" in web_results.lower() or "no relevant web results" in web_results.lower():
        return output

    external_marker = '<span style="color: #1565c0; font-style: italic;">External Context:</span>'
    if external_marker in output:
        prefix = output.split(external_marker, 1)[0].rstrip()
        formatted_results = web_results.replace("\n\n---\n\n", "\n\n")
        return (
            f"{prefix}\n\n{external_marker} Available.\n\n"
            f"{formatted_results}"
        )

    return output


def _to_agent_messages(question: str, chat_history: list | None = None) -> list[dict[str, str]]:
    """Build graph input messages from chat history plus the current question."""
    messages = []
    for role, content in (chat_history or []):
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})
    return messages


def _content_text(content) -> str:
    """Extract visible text from string or structured LangChain content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
        return "".join(text_parts)
    return ""


def _final_ai_output(messages: list) -> str:
    """Extract the final non-tool AI message content from a graph trace."""
    ai_messages = [
        m for m in messages
        if hasattr(m, "type") and m.type == "ai" and m.content and not m.tool_calls
    ]
    return _content_text(ai_messages[-1].content) if ai_messages else ""


def _stream_chunk_text(chunk) -> str:
    """Extract visible text from a LangChain AIMessageChunk."""
    return _content_text(getattr(chunk, "content", ""))


def _full_chunk_texts(messages: list) -> list[str]:
    """Collect full local chunk texts from the tool messages in a graph trace.

    audit.extract_retrieved_sources caps each excerpt at MAX_EXCERPT_CHARS for
    the audit record; grounding validation needs the whole chunk. Same block
    format and whitespace normalization as the audit parse, no cap.
    """
    texts = []
    for message in messages:
        if isinstance(message, dict):
            message_type = str(message.get("type", ""))
            content = message.get("content", "")
        else:
            message_type = str(getattr(message, "type", ""))
            content = getattr(message, "content", "")
        if message_type != "tool" or not isinstance(content, str):
            continue
        for block in content.split("\n\n---\n\n"):
            match = SOURCE_HEADER_RE.match(block.strip())
            if match:
                texts.append(" ".join(match.group("content").split()))
    return texts


def attach_full_chunk_content(sources: list, messages: list) -> list:
    """Return source copies whose full chunk text rides a ``content`` key.

    The audit record keeps the capped ``excerpt`` convention (audit.py owns
    that cap); the uncapped text is attached to copies for grounding only, so
    a number sitting past the excerpt cap still counts as supported. Each full
    text is matched to its source by excerpt prefix; sources with no match
    (web results, synthetic fixtures) pass through unchanged.
    """
    full_texts = _full_chunk_texts(messages)
    enriched = []
    for source in sources:
        excerpt = str(source.get("excerpt", ""))
        full = next(
            (text for text in full_texts if excerpt and text.startswith(excerpt)),
            None,
        )
        enriched.append({**source, "content": full} if full else dict(source))
    return enriched


def _held_review_notice(review_id: str, risk_level: str) -> str:
    """User-facing message shown in place of a held draft answer (hold mode)."""
    return (
        f"This response is held for human review because {risk_level} risk was "
        "flagged. A reviewer will assess it before release. "
        f"Review ID: {review_id}."
    )


def _build_review_item(
    audit_id: str | None,
    question: str,
    draft_answer: str,
    risk_result: dict,
    sources: list,
) -> dict:
    """Shape a review queue item from the held answer and its risk signals.

    riskReasons come from the scorer result here, not from the report (the report
    carries riskScore/riskLevel only, per §9.3). reviewId is derived from auditId
    for 1:1 traceability back to the audit record. wasWithheld snapshots the
    hold/flag mode in effect when the item was created; items written before this
    field lack the key and readers must treat that as null.
    """
    return {
        "reviewId": f"review_{audit_id}",
        "auditId": audit_id,
        "question": question,
        "draftAnswer": draft_answer,
        "riskScore": risk_result.get("risk_score", 0.0),
        "riskLevel": risk_result.get("risk_level", "low"),
        "riskReasons": list(risk_result.get("risk_reasons", [])),
        "retrievedSources": sources,
        "decision": "held_for_review",
        "reviewStatus": "pending",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "wasWithheld": config.HUMAN_REVIEW_HOLD,
    }


def _finalize_query_result(
    question: str,
    output: str,
    result_messages: list,
    trace_messages: list,
    guardrail_outcome: str | None = None,
    context_admission: AdmissionSummary | None = None,
    audit_id: str | None = None,
) -> dict:
    """Extract sources, validate grounding, write audit logs, and shape the result.

    audit_id, when provided, was pre-generated by the caller and already attached
    to the run's trace metadata; the audit record reuses it so the trace and the
    audit log share one join key.
    """
    sources = extract_retrieved_sources(result_messages)
    if guardrail_outcome is None:
        guardrail_outcome = detect_guardrail_intervention(output, result_messages)

    # Runtime governance: grounding score, risk score, and the per-answer report.
    # Grounding runs against the full retrieved set (the evidence the answer was
    # built from), not the display-filtered sources below. Validation reads the
    # whole chunk text; the audit record and everything downstream of it keep
    # the capped excerpts.
    evidence_sources = attach_full_chunk_content(sources, result_messages)
    grounding_result = validate_grounding(output, evidence_sources)
    risk_result = score_risk(
        grounding_result["grounding_score"],
        guardrail_outcome,
        external_context_used(output),
    )

    written_audit_id = None
    audit_error = None
    governance_report = None
    try:
        audit_record = build_audit_record(
            query=question,
            answer=output,
            messages=trace_messages,
            retrieved_sources=sources,
            audit_id=audit_id,
        )
        if guardrail_outcome:
            audit_record["guardrail_outcome"] = guardrail_outcome
        governance_report = build_report(
            audit_id=audit_record["audit_id"],
            model=config.CHAT_MODEL_ID,
            retrieved_chunks=sources,
            response_text=output,
            guardrail_outcome=guardrail_outcome,
            grounding_result=grounding_result,
            risk_result=risk_result,
            context_admission=context_admission or AdmissionSummary(),
        )
        audit_record["governance_report"] = governance_report
        written_audit_id = write_audit_record(audit_record)
    except OSError as exc:
        audit_error = str(exc)

    # Phase 5: a high-risk answer is held for the human review queue. The draft is
    # captured into the queue item BEFORE any user-facing substitution. In hold
    # mode the user-facing answer becomes a short notice; the audit log keeps the
    # draft (built above with answer=output). Blocked answers never reach here
    # because the categorical override zeroes humanReviewRequired and sets
    # decision="blocked".
    user_facing_output = output
    if governance_report and governance_report.get("decision") == "held_for_review":
        review_item = _build_review_item(
            audit_id=governance_report.get("auditId"),
            question=question,
            draft_answer=output,
            risk_result=risk_result,
            sources=sources,
        )
        try:
            review_queue.enqueue(review_item, config.REVIEW_QUEUE_DIR)
        except OSError as exc:
            audit_error = audit_error or str(exc)
        if config.HUMAN_REVIEW_HOLD:
            user_facing_output = _held_review_notice(
                review_item["reviewId"], review_item["riskLevel"]
            )

    return {
        "output": user_facing_output,
        "messages": result_messages,
        "sources": [] if guardrail_outcome == "blocked" else (sources if should_expose_retrieved_sources(output) else []),
        "audit_id": written_audit_id,
        "audit_error": audit_error,
        "guardrail_outcome": guardrail_outcome,
        "governance_report": governance_report,
    }


def stream_query(question: str, chat_history: list | None = None) -> Iterator[dict]:
    """Stream a question through the agent as UI-friendly events.

    The first model pass may contain tool-planning prose. We buffer visible text
    until the final-answer marker appears so the UI streams the answer itself,
    not intermediate tool planning.
    """
    admission = AdmissionSummary()
    # audit_id is pre-generated so the LangSmith trace (when LANGSMITH_TRACING is
    # enabled) and the audit record share one join key. metadata/tags ride the
    # standard RunnableConfig and are inert when tracing is off.
    audit_id = str(uuid.uuid4())
    run_config = {
        "configurable": {"admission_stash": admission},
        "metadata": {"audit_id": audit_id},
        "tags": ["react-rag"],
    }
    graph = build_agent(streaming=True)
    messages = _to_agent_messages(question, chat_history)
    result_messages: list = []
    trace_messages: list = []
    streamed_parts: list[str] = []
    pending_text = ""
    answer_started = False
    final_output = ""

    yield {"type": "status", "message": "Searching local documents..."}

    for mode, payload in graph.stream(
        {"messages": messages},
        run_config,
        stream_mode=["messages", "updates"],
    ):
        if mode == "messages":
            chunk, _metadata = payload
            text = _stream_chunk_text(chunk)
            if not text:
                continue

            if answer_started:
                streamed_parts.append(text)
                yield {"type": "token", "content": text}
                continue

            pending_text += text
            marker_index = pending_text.lower().find("## result summary")
            if marker_index == -1:
                marker_index = pending_text.lower().find("result summary")
            if marker_index != -1:
                answer_started = True
                visible_text = pending_text[marker_index:]
                streamed_parts.append(visible_text)
                yield {"type": "status", "message": "Composing answer..."}
                yield {"type": "token", "content": visible_text}
            continue

        if mode != "updates":
            continue

        for node_update in payload.values():
            update_messages = list(node_update.get("messages", []))
            if not update_messages:
                continue
            trace_messages.extend(update_messages)
            result_messages.extend(update_messages)

            if any(getattr(message, "type", None) == "tool" for message in update_messages):
                yield {"type": "status", "message": "Composing answer..."}

            output = _final_ai_output(update_messages)
            if output:
                final_output = output

    output = final_output or "".join(streamed_parts)
    guardrail_outcome = detect_guardrail_intervention(output, result_messages)

    # A guardrail block (and any answer that never hits the "## Result Summary"
    # marker) streams zero visible tokens above. Surface the final text once here
    # so the UI shows the message instead of an endless "Preparing answer..."
    # state. Normal answers set answer_started and skip this.
    if not answer_started and output:
        yield {"type": "replace", "content": output}

    if guardrail_outcome != "blocked" and should_run_external_fallback(question, output):
        yield {"type": "status", "message": "Searching external context..."}
        web_results = web_search.invoke(question, run_config)
        updated_output = apply_external_fallback(output, web_results)
        trace_messages.append(
            {
                "type": "tool",
                "name": "web_search",
                "content": web_results,
            }
        )
        if updated_output != output:
            output = updated_output
            yield {"type": "replace", "content": output}

    result = _finalize_query_result(
        question=question,
        output=output,
        result_messages=result_messages,
        trace_messages=trace_messages,
        guardrail_outcome=guardrail_outcome,
        context_admission=admission,
        audit_id=audit_id,
    )

    # A held answer in hold mode comes back from finalize with the user-facing
    # text swapped for the held notice. The draft already streamed token by token
    # (the marker was present, so answer_started is True and the block above did
    # not fire), so replace it with the notice. The governance_report event below
    # still carries decision=held_for_review.
    if result["output"] != output:
        yield {"type": "replace", "content": result["output"]}

    yield {"type": "sources", "sources": result["sources"]}
    yield {"type": "audit_id", "audit_id": result["audit_id"]}
    yield {"type": "governance_report", "report": result["governance_report"]}
    if result.get("audit_error"):
        yield {"type": "warning", "message": result["audit_error"]}
    yield {"type": "done"}


def query(question: str, chat_history: list | None = None) -> dict:
    """Run a single question through the agent and return the result."""
    admission = AdmissionSummary()
    # Same trace ↔ audit-log join key wiring as stream_query.
    audit_id = str(uuid.uuid4())
    run_config = {
        "configurable": {"admission_stash": admission},
        "metadata": {"audit_id": audit_id},
        "tags": ["react-rag"],
    }
    graph = build_agent()
    messages = _to_agent_messages(question, chat_history)

    result = graph.invoke({"messages": messages}, run_config)

    output = _final_ai_output(result["messages"])
    trace_messages = list(result["messages"])

    guardrail_outcome = detect_guardrail_intervention(output, result["messages"])

    if guardrail_outcome != "blocked" and should_run_external_fallback(question, output):
        web_results = web_search.invoke(question, run_config)
        output = apply_external_fallback(output, web_results)
        trace_messages.append(
            {
                "type": "tool",
                "name": "web_search",
                "content": web_results,
            }
        )

    return _finalize_query_result(
        question=question,
        output=output,
        result_messages=result["messages"],
        trace_messages=trace_messages,
        guardrail_outcome=guardrail_outcome,
        context_admission=admission,
        audit_id=audit_id,
    )
