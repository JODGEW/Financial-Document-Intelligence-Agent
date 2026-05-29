# Bedrock Guardrails Policy

This policy defines the Amazon Bedrock Guardrail applied to every model invocation
in the ReAct-RAG agent. The guardrail enforces the rules below at the model
boundary — independent of the system prompt — so a prompt-injection or a
mis-tuned LLM cannot bypass them.

The provisioning script [scripts/setup_guardrail.py](../scripts/setup_guardrail.py)
creates the guardrail from this policy. The active guardrail ID and version are
set in environment variables `BEDROCK_GUARDRAIL_ID` and `BEDROCK_GUARDRAIL_VERSION`
(see [config.py](../config.py)). When unset, the agent runs without guardrails
and emits a one-line warning at startup.

This file lives outside `/docs` deliberately so the ingestion pipeline does not
index it as a corpus document.

## 1. Denied topics

The agent must refuse the following topic categories regardless of question
phrasing. On a match, Bedrock blocks the response and returns a
`GUARDRAIL_INTERVENED` stop reason. The agent surfaces this as a structured
"blocked by policy" answer to the user with the matched policy id.

| ID | Topic | Definition |
| --- | --- | --- |
| `personalized_investment_advice` | Personalized buy/sell/hold or price-prediction advice | Definition (capped at ~200 chars by Bedrock): "Personalized buy/sell/hold recommendations or future-price predictions directed at the user. Excludes factual lookup of disclosed filings or historical figures." The "Excludes…" clause and the absence of named-ticker examples are deliberate — earlier wording false-positived on legitimate filing Q&A like "What was Acme's FY2025 revenue?". |
| `legal_opinion` | Legal opinion on filings | Providing a legal opinion on the adequacy, compliance, or liability exposure of a specific filing or disclosure. The agent is allowed to summarize what a filing *says*, not to opine on whether it satisfies a legal standard. |
| ~~`mnpi_request`~~ | (removed) | Originally included to block requests for pre-announcement earnings, unannounced M&A, and unfiled drafts. Removed after live testing because Bedrock's topic classifier keyed on tokens like "earnings per share" / "filings" regardless of the carve-out wording, causing false positives on every legitimate 10-K question. This corpus carries no real MNPI (public sample filings + a compliance policy), so the topic was net-negative. **Re-introduce only when** the corpus starts holding drafts or pre-release content, and prefer **input-side classification** over a topic policy for this category. |

## 2. Sensitive information filters (PII)

PII categories below are masked in agent output. Inputs containing these PII
categories are passed through (the agent may need them for retrieval) but the
audit log records that a filter fired.

| Category | Action |
| --- | --- |
| `EMAIL` | Mask in output (`{EMAIL}`) |
| `PHONE` | Mask in output (`{PHONE}`) |
| `US_SOCIAL_SECURITY_NUMBER` | Mask in output (`{SSN}`) |
| `CREDIT_DEBIT_CARD_NUMBER` | Mask in output (`{CARD}`) |
| `ADDRESS` | Mask in output (`{ADDRESS}`) |

## 3. Contextual grounding checks

Bedrock's contextual grounding check scores each response against the retrieved
context. Responses below threshold are blocked.

| Check | Threshold | Notes |
| --- | --- | --- |
| `GROUNDING` | 0.65 | Each sentence in the answer must be supported by retrieved context above this score. |
| `RELEVANCE` | 0.50 | The response must be relevant to the user's query above this score. |

These thresholds intentionally trail the deterministic eval runner's stricter
rule-based checks; the guardrail is the runtime safety net, not the primary
quality bar.

## 4. Operator notes

- **Cost / latency.** Each guardrail evaluation adds approximately 100–300 ms per
  turn and a per-request charge (see Bedrock pricing for the active region).
- **Versioning.** Update this document, then run `scripts/setup_guardrail.py`
  with `--bump-version` to publish a new guardrail version. Update
  `BEDROCK_GUARDRAIL_VERSION` in the deployment environment to roll forward.
- **Audit.** Every blocked response writes a record to
  `audit_logs/query_audit.jsonl` with `guardrail_outcome="blocked"` and the
  matched topic / filter category, so reviewers can sample blocks and tune
  thresholds.
