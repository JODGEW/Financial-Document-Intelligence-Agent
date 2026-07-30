# Financial Document Intelligence Agent

A Python RAG agent for regulated-document workflows in finance and compliance.

This project answers questions over a local corpus of financial and policy documents using LangChain, Chroma, and AWS Bedrock. It uses retrieval over local documents as the primary source of truth and can fall back to Tavily for clearly separated public-web context when the local corpus does not contain the needed information.

The goal is to demonstrate the architecture behind document intelligence systems used in compliance, investment research, and analyst workflows:
- retrieval over dense document sets
- source-aware answers from internal documents
- optional public-web research as supplemental context
- agent-based multi-step reasoning across tools

## Run the web UI

To make the browser visualization work, run these two commands in two separate
terminals:

```bash
# Terminal 1: backend API, from the repo root
python -m uvicorn api:app --reload
```

```bash
# Terminal 2: frontend UI, from the repo root
cd frontend && npm run dev
```

Then open `http://localhost:5173` in your browser. The frontend needs the
backend running at `http://127.0.0.1:8000`; otherwise the document sidebar will
not load.

### Frontend preview

**Grounded answer with retrieved source evidence**

![Grounded document answer with citations and retrieved evidence](assets/screenshots/frontend-grounded-answer.png)

**Governance validation and decision trace**

![Expanded governance report showing source usage, grounding validation, risk, and decision](assets/screenshots/frontend-governance-report.png)

## Why this project

In regulated workflows, a plain chatbot answer is not enough. Users need answers tied back to source material they can inspect.

This project is designed around that idea:
- local documents are the primary source of truth
- answers should be grounded in retrieved evidence
- external web results should be labeled as external context
- document metadata should support traceability and review

This is not a production compliance system or legal tool. It is a portfolio project that demonstrates the engineering patterns behind one.

---

## What it currently does

- [x] Loads a local document corpus from `/docs` (10 formats / 14 extensions: .pdf, .md, .txt, .docx, .xlsx, .xls, .html, .htm, .csv, .pptx, .eml, .msg, .json, .jsonl)
- [x] Chunks and embeds documents into a persistent Chroma vector store
- [x] Uses an agent to choose between local retrieval and web search
- [x] Returns answers in a structured Result Summary format
- [x] Preserves source metadata from local documents
- [x] Clearly separates internal corpus answers from external web context
- [x] Refuses to substitute one company's data for another
- [x] Web results include clickable markdown links with source, date, headline
- [x] Gracefully handles missing Tavily API key (web search disabled, no crash)
- [x] Thread-safe Chroma singleton for parallel tool calls
- [x] Supports CLI, Gradio fallback, and React/TypeScript interfaces
- [x] Streams workflow status while the answer is generated and validated, then releases the buffered, governance-decided answer in one update (drafts and withheld source excerpts are never transmitted before the decision)
- [x] Returns a safe, stable error contract from the chat API (`code` + generic message + opaque correlation id); full exception detail stays in the server log
- [x] Fails startup explicitly when a governance policy YAML file is present but invalid; a missing policy file uses the documented built-in defaults
- [x] Review-queue writes are atomic per file (temp file + fsync + rename) and race-safe within one process; the pending→terminal move is not a database transaction and the CLI racing the API from a second process is unprotected
- [x] Caps Gradio chat history to prevent stale formatting from older turns
- [x] Exposes retrieved chunk metadata in the React answer view
- [x] Extracts PDF table text into retrievable chunks when PyMuPDF detects tables
- [x] Tracks document version metadata and supports retrieval filters by company, filing type, and year
- [x] Writes JSONL audit logs for queries, retrieved sources, and response traces
- [x] Includes a deterministic evaluation runner for retrieval, citations, grounding, unsupported claims, routing, and latency
- [x] Wraps the agent in a governance layer: context policy admission, runtime grounding validation, per-answer governance report, and document change impact analysis
- [x] Holds answers that meet a mandatory-review policy condition in a human review queue — either weighted risk at/above the 0.75 threshold or grounding strictly below the 0.50 review floor, each with an explicit reason code (a low weighted-risk answer can still be held by the floor). Hold or flag mode via `HUMAN_REVIEW_HOLD`; resolved in the React reviewer workspace or the CLI (approve/reject with an optional note — no draft editing yet)
- [x] Optional LangSmith run tracing (off by default); every traced run carries the answer's `audit_id`, joining it to the audit log and governance report
- [x] 884 tests covering multi-format ingestion, per-format loaders, PII redaction, tools, agent behavior, auditability, API responses and error sanitization, evaluation metrics, governance reporting and policy validation, context policy admission, runtime grounding validation, streaming leak prevention, document change impact analysis, the human review queue (routes, atomicity, races, demo seeding), durable comparison-detection jobs, local access control, and thread safety

## Best-fit use cases

The current implementation is best suited for:
- SEC 10-K and 10-Q filings
- earnings call transcripts
- internal compliance manuals
- policy documents
- governance and controls documentation
- research notes
- board minutes, committee charters, proxy statements, training decks (DOCX/PPTX)
- control matrices, risk registers, audit trails, KYC records (XLSX/CSV)
- regulatory correspondence, email archives (EML/MSG)
- XBRL inline filings, API data exports (JSON/JSONL)
- other dense PDF / markdown / text corpora

---

## Quickstart

```bash
cp .env.example .env
# Edit .env with your AWS credentials and optional Tavily API key

pip install -r requirements.txt
cd frontend && npm install && cd ..

# Add documents to the corpus (sample docs are included)
# cp my-docs/*.pdf docs/

python ingest.py
python cli.py -q "What does the personal trading policy require before executing a trade?"
```

Other ways to run:

```bash
python cli.py                    # interactive CLI mode
python app.py                    # Gradio fallback UI
python -m uvicorn api:app --reload  # React API, run from repo root
cd frontend && npm run dev          # React UI, run in a second terminal
python -m pytest tests/ -v       # run tests
python eval_runner.py            # run evaluation dataset
python scripts/demo_walkthrough.py  # guided governance demo (see below)
```

### Example output

```text
## Result Summary

**Internal Corpus Answer:** Available in current local corpus.

According to Acme Corporation's personal trading policy, all covered persons
must obtain pre-clearance from the Compliance Department before executing any
trade in Acme Corporation securities, securities on the Restricted List, or any
security about which they possess material nonpublic information (MNPI).
Pre-clearance requests must be submitted at least 24 hours before the intended
trade date, and approvals are valid for 48 hours.
[compliance-policy-personal-trading.md]

**External Context:** Unavailable.
```

The agent uses a structured **Result Summary** format for every answer:
- **Internal Corpus Answer** — grounded in local documents with source citations
- **External Context** — clearly separated web results, or marked unavailable

---

## Guided demo

One command walks the governance story end to end, pausing between acts:

```bash
python scripts/demo_walkthrough.py
```

1. **A grounded answer** — one real query, then the governance report behind it: which chunks the context policy admitted or dropped (and why), grounding score, citation coverage, risk score, and the routing decision.
2. **A held answer** — a deliberately ungrounded draft (fabricated numbers, citation to a file that was never retrieved) is validated by the real pipeline under the normal policy; the mandatory grounding floor holds it: the user sees a held notice instead of the draft, the full draft lands in the review queue with its evidence and reason codes, and the reviewer CLI approves it. No thresholds are modified.
3. **Drift, caught** — the eval baseline is diffed against a perturbed temp copy (the real `eval/baseline.json` is untouched), producing the exact `scripts/eval_diff.py` output an operator sees when a metric moves, a case flips pass/fail, or latency shifts. Add `--live-eval` to run the real eval against Bedrock instead of the offline diff.

Acts 1–2 need AWS credentials and an ingested corpus; Act 3 in default mode needs neither. `--no-pause` runs straight through. The demo's one persistent side effect is intentional — Act 2 writes a real item into `review_queue/` (gitignored runtime state; a clean clone starts with an empty queue). Seed or reset local demo queue data with `python scripts/review_queue.py seed --reset`.

---

## Architecture

```text
                    ┌──────────────────────────────┐
   User question ──▶│        Agent Executor        │
                    │     (AWS Bedrock model)      │
                    └───────────┬──────────┬───────┘
                                │          │
                     ┌──────────▼───┐  ┌───▼──────────┐
                     │ Local Search │  │  Web Search  │
                     │    Tool      │  │    Tool      │
                     └───────┬──────┘  └──────────────┘
                             │
                    ┌────────▼─────────┐
                    │ Chroma Vector DB │
                    └────────┬─────────┘
                             │
                  Embedded chunks from /docs

Ingestion:   /docs ──▶ loader ──▶ splitter ──▶ embed ──▶ Chroma
Query:       question ──▶ agent ──▶ tool(s) ──▶ observe ──▶ answer
```

The governance layer wraps that agent loop. Context policy decides what reaches
the model before retrieval returns; grounding, risk, and the report run after the
answer is generated:

```text
question
   │
   ▼
[context policy] ── admits/drops chunks by token budget, freshness, source
   │
   ▼
   agent loop (local_search / web_search ──▶ Bedrock model)
   │
   ▼
[grounding validation] ── citation coverage, grounding score, unsupported claims
   │
   ▼
[risk scorer] ── 3-signal score; blocked answers scored categorically (risk=high)
   │
   ▼
[governance report] ── audit ID, source usage, validation, risk, decision
   │
   ▼
answer  +  governance report  (API, React, CLI summary)  +  JSONL audit log
```

### Components

**Agent Executor**
Given a question, the agent decides which tool to call, inspects the result, and determines whether it has enough context to answer.

**Local Search Tool**
Wraps the Chroma retriever. The agent uses it to search the internal document corpus and return relevant chunks with source metadata.

**Web Search Tool**
Uses Tavily for supplemental public-web context. In finance-oriented workflows, this should be treated as external context, not a replacement for internal evidence.

**Governance path**
Amazon Bedrock Guardrails are wired end-to-end. The policy ([policies/guardrails-policy.md](policies/guardrails-policy.md)) defines denied topics (personalized investment advice, legal opinions, MNPI requests), PII anonymization (email, phone, SSN, card, address), and contextual grounding/relevance thresholds. Provision once with [scripts/setup_guardrail.py](scripts/setup_guardrail.py), then set `BEDROCK_GUARDRAIL_ID` and `BEDROCK_GUARDRAIL_VERSION` in the environment. When the guardrail intervenes, the agent surfaces a structured "blocked by policy" answer, suppresses the external-web fallback, and writes `guardrail_outcome="blocked"` into the audit log. The eval runner scores `guardrail_block_accuracy` from a denied-topic probe case so regressions in enforcement are visible.

---

## AI Governance Layer

The project wraps the document agent in a governance layer. It controls what information enters the model, validates whether each answer is grounded in retrieved evidence, scores risk, and produces a governance report for every answer, persisted with the append-only JSONL audit record (a local file, not tamper-proof storage). The report is visible in the API response, the React panel, and a one-line CLI summary, and the full record is written to the JSONL audit log.

The point is to make each answer defensible: you can see which chunks were admitted or dropped and why, how well the answer is grounded, what the risk decision was, and which past answers depend on a document that just changed.

Currently supports:

- governance report per answer (audit ID, source usage, validation, risk, decision)
- context policy admission with drop reasons and token budgets
- runtime grounding validation — deterministic and lexical (citation-string matching plus numeric-token support), not semantic entailment (citation coverage, grounding score, unsupported numeric claims)
- categorical block scoring (blocked answers report risk=high, validation=N/A)
- document change impact analysis CLI (`scripts/document_impact.py`) — same-document changed-chunk analysis against past answers; not a two-period filing comparison
- human review queue with a reachable mandatory-review policy: `held_for_review` routing driven by the weighted 0.75 threshold or the 0.50 grounding floor (explicit reason codes in the report and queue item), hold or flag mode gated by `HUMAN_REVIEW_HOLD`, CLI approve/reject via `scripts/review_queue.py`, and a reviewer workspace in the React UI over the `/api/reviews` routes
- internal-first retrieval policy
- external context separation
- document version and chunk traceability
- Bedrock Guardrails for denied topics and PII handling
- JSONL audit logs for query and retrieval traces
- deterministic evaluation for grounding, citations, routing, and drift (32-case dataset with a frozen baseline)
- optional LangSmith run tracing, joined to the audit log by `audit_id`

Planned next: the financial filing comparison workflow (see the roadmap under "What would make this more production-like"). Document-level permissions — formerly "Phase 6" — are deferred until after the comparison workflow and basic authentication.

The governance modules live in [governance/](governance/). The full design doc and phase-by-phase build log are kept as local working notes outside the public repo.

---

## Review workspace

The React UI has a second workspace for working the human review queue. A "Review Queue" entry in the top bar switches between chat and review; the switch is plain local state (no router, no deep links, and a refresh returns to chat).

![Review workspace showing the pending queue and a held answer's detail: risk reasons, the withheld draft with internal and external context, and retrieved evidence](assets/screenshots/review-workspace.png)

*The items and risk labels in this screenshot predate the current risk policy. Generate equivalent local demo state with `python scripts/review_queue.py seed` — one pending, one approved, and one rejected item produced through the real validation and resolution path.*

What it does:

- lists queue items by status (Pending / Approved / Rejected / All), in the server's order
- shows the full item: risk signals, the original question, the held draft rendered with the same rules as a chat answer, the governance report, the retrieved context with links into the corpus, and the item's identifiers
- records approve/reject decisions with an optional reviewer note behind a confirmation dialog
- shows whether the answer was withheld before delivery (the `wasWithheld` snapshot; items written before that field existed read as "delivery state unknown")

Deliberate limits: this generic answer-review workspace is a local demo
surface and remains outside the comparison-workflow authentication boundary.
It has no authenticated reviewer identity, assignment, or SLA; the only
actions are approve and reject with an optional note (no draft editing).
Approving a draft does not deliver it to the original requester — a decision
only moves the item between queue files and stamps `reviewedAt` plus the note.
Persistence is local JSONL: writes are atomic per file and race-safe within one
process, but the pending→terminal move is not a transaction and a second
process (e.g. the CLI racing the API) is unprotected. A clean clone starts with
an empty queue; `python scripts/review_queue.py seed` creates demo items. The
workspace banner states these limits.

The API behind it:

| Route | Purpose |
| --- | --- |
| `GET /api/reviews` | List review items by `status` query param (`pending` default, `approved`, `rejected`, `all`). Pending sorts oldest first; resolved items sort most recently reviewed first. |
| `GET /api/reviews/{review_id}` | Full item detail across all statuses. Joins the audit log by `auditId` and returns only the record's `governance_report`. |
| `POST /api/reviews/{review_id}/approve` | Approve a pending item. Body `{"note": "..."}` is optional. |
| `POST /api/reviews/{review_id}/reject` | Reject a pending item, same body. |

Responses are allowlisted DTOs: corpus-relative paths only (the stored absolute `source` path never leaves the server), `404` for unknown ids, `409` when the item was already resolved, `422` for invalid status values or a malformed body.

---

## Tech Stack

| Layer            | Technology                          |
| ---------------- | ----------------------------------- |
| Language         | Python 3.10                         |
| Orchestration    | LangChain                           |
| LLM              | Claude Haiku 4.5 via AWS Bedrock    |
| Embeddings       | Titan Embeddings v2 via AWS Bedrock |
| Vector Store     | Chroma                              |
| Document Parsing | PyMuPDF (PDF), python-docx (DOCX), openpyxl (XLSX), BeautifulSoup4 + lxml (HTML), python-pptx (PPTX), extract-msg (.msg), stdlib (TXT/MD/CSV/EML/JSON) |
| Web Research     | Tavily Search / Research            |
| Interface        | CLI + Gradio + React/TypeScript     |

---

## Example finance/compliance workflows

### 1. Filing Q&A

**Question:**
"What are Acme Corp's key risk factors?"

**Expected behavior:**
The agent searches the local corpus, retrieves risk-factor chunks from the 10-K PDF, and answers with page-level citations.

### 2. Policy lookup

**Question:**
"What does the compliance policy say about blackout periods for personal trading?"

**Expected behavior:**
The agent retrieves the relevant section from the compliance policy markdown and answers using grounded context.

### 3. Cross-document analysis

**Question:**
"How do the cybersecurity risks in the 10-K compare to the trends described in the internal research note?"

**Expected behavior:**
The agent retrieves relevant chunks from both the filing and the research note and synthesizes a comparison.

### 4. Internal + external context

**Question:**
"Compare Acme Corp's risk disclosures with any recent public news about cybersecurity incidents."

**Expected behavior:**
The agent retrieves local evidence first, then uses web search for clearly separated external context.

---

## Project Structure

```text
ReAct-RAG/
├── config.py        # central config (Bedrock models, chunking, paths)
├── ingest.py        # load → chunk → embed → persist (Chroma)
├── loaders/         # format-handler registry + per-format loaders
│   ├── __init__.py        # auto-imports all handler modules
│   ├── registry.py        # FormatHandler + register / handler_for / supported_extensions
│   ├── pii.py             # email/SSN/card/phone regexes + redact / should_redact
│   ├── tables.py          # format_table_rows + per-format extract_*_tables helpers
│   ├── common.py          # humanize_stem (filename → human title with acronym preservation)
│   ├── pdf.py / markdown.py / text.py
│   ├── docx.py / excel.py / html.py / csv.py
│   └── pptx.py / email_doc.py / json_doc.py
├── tools.py         # local_search (Chroma) + web_search (Tavily, optional)
├── audit.py         # retrieved-source extraction + JSONL audit logging
├── agent.py         # LangChain agent (LangGraph + tool-calling loop)
├── eval_runner.py   # deterministic evaluation metrics and report
├── cli.py           # CLI (single query + interactive mode)
├── app.py           # Gradio fallback chat UI
├── api.py           # FastAPI backend for the React UI
├── eval/            # sample questions, expectations, and frozen baseline.json for drift checks
├── frontend/        # React + TypeScript + Tailwind UI (chat + review workspaces)
├── docs/            # sample docs (.md, .txt, .pdf) — corpus only; never indexed: policies/
├── policies/        # safety policy docs (e.g. guardrails-policy.md) — outside corpus
├── scripts/         # operator scripts (setup_guardrail.py, eval_diff.py, document_impact.py, review_queue.py, demo_walkthrough.py)
├── chroma_db/       # persistent vector store
├── governance/      # governance layer (report, grounding, risk, context policy, impact, review queue)
│   ├── governance_report.py  # per-answer report assembly
│   ├── grounding_validator.py # runtime citation + claim grounding
│   ├── risk_scorer.py        # 3-signal risk score + categorical block override
│   ├── context_policy.py     # chunk admission, token budgets, drop reasons
│   ├── impact.py             # document change diff + audit log scan
│   └── review_queue.py       # file-backed human review queue (enqueue/list/approve/reject + cross-status reads)
├── tests/           # 884 tests (including durable detection jobs, comparison regression, reliability, authenticated actors, and access control)
│   ├── test_ingest.py
│   ├── test_tools.py
│   ├── test_agent.py
│   ├── test_audit.py
│   ├── test_api.py
│   ├── test_eval_runner.py
│   ├── test_guardrails.py
│   ├── test_governance_report.py
│   ├── test_grounding_validator.py
│   ├── test_risk_scorer.py
│   ├── test_context_policy.py
│   ├── test_document_impact.py
│   ├── test_review_queue.py
│   ├── test_agent_review.py
│   ├── test_loaders_registry.py
│   ├── test_pii_redaction.py
│   └── test_loader_{docx,excel,html,csv,pptx,email,json}.py
├── requirements.txt # dependencies
└── .env.example     # template for environment variables
```

---

## Environment and access

| Variable                | Required | Notes                                                    |
| ----------------------- | -------- | -------------------------------------------------------- |
| `AWS_ACCESS_KEY_ID`     | Yes*     | For local development if not using a profile or IAM role |
| `AWS_SECRET_ACCESS_KEY` | Yes*     | For local development if not using a profile or IAM role |
| `AWS_REGION`            | Yes      | Example: `us-east-1`                                     |
| `CHAT_MODEL_ID`         | No       | Bedrock chat model. Default `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| `EMBEDDING_MODEL_ID`    | No       | Bedrock embedding model. Default `amazon.titan-embed-text-v2:0` |
| `TAVILY_API_KEY`        | No       | Needed only for web search                               |
| `BEDROCK_GUARDRAIL_ID`  | No       | Enables Bedrock Guardrails when set; unset means guardrails are off |
| `BEDROCK_GUARDRAIL_VERSION` | No   | Guardrail version to apply. Required alongside `BEDROCK_GUARDRAIL_ID` |
| `BEDROCK_GUARDRAIL_TRACE` | No     | Guardrail trace level. Default `ENABLED`                 |
| `CHROMA_COLLECTION`     | No       | Defaults to `documents`                                  |
| `AUDIT_LOG_PATH`        | No       | Defaults to `audit_logs/query_audit.jsonl`               |
| `HUMAN_REVIEW_HOLD`     | No       | Default `true`. When true, high-risk answers are withheld and replaced with a review notice; when false, the answer returns but the item is still enqueued for review |
| `REVIEW_QUEUE_DIR`      | No       | Review queue directory. Defaults to `review_queue/`, anchored to the module dir like `AUDIT_LOG_PATH` |
| `LANGSMITH_TRACING`     | No       | Set to `true` to send run traces to LangSmith. Off by default; the agent behaves identically when unset |
| `LANGSMITH_API_KEY`     | No       | LangSmith API key. Required only when `LANGSMITH_TRACING=true` |
| `LANGSMITH_PROJECT`     | No       | LangSmith project name for traces. Defaults to `default`. Every traced run carries the answer's `audit_id` in its metadata, joining it to the audit log and governance report |
| `PII_REDACT_AT_INGEST`  | No       | Global PII redaction on load. Default `false`            |
| `PII_REDACT_TABULAR_AT_INGEST` | No | Tabular-only PII redaction for CSV/XLSX. Default `true`  |
| `FDIA_AUTH_SECRET`      | Yes**    | Local comparison-API signing secret; at least 32 UTF-8 bytes, with no default, placeholder, or low-diversity value |

\* In deployed environments, IAM roles are the preferred access pattern for Bedrock and related AWS services.

\*\* Required when constructing the API, which fails closed at startup when
the secret is missing or invalid. Detector, ingestion, evaluation, regression,
and other library-only commands do not need it.

### Comparison API access

Every comparison-workflow route requires an
`Authorization: Bearer <access-token>` header. The local issuer creates signed
HS256 JWT access tokens; verification fixes the algorithm and requires
`sub`, `roles`, `iss`, `aud`, `iat`, `nbf`, `exp`, `jti`, and `typ=access`.
Permissions are derived from the checked-in
[access-control policy](policies/access_control_policy.yaml), never accepted
from token claims. The policy defines exact permissions for the `viewer`,
`operator`, `reviewer`, `exporter`, and `admin` roles. Missing, invalid, or
expired credentials return a sanitized `401` with
`WWW-Authenticate: Bearer`; a verified principal without the route's exact
permission receives a sanitized `403`.

| Role | Exact grants |
| --- | --- |
| `viewer` | `comparison.read`, `detection_attempt.read`, `recovery.read`, `reliability.read`, `governance.read`, `review.read`, `export.read` |
| `operator` | all viewer grants plus `comparison.create`, `comparison.detect`, `recovery.replay`, `governance.evaluate` |
| `reviewer` | all viewer grants plus `review.decide` |
| `exporter` | all viewer grants plus `export.create` |
| `admin` | all 13 defined permissions |

The policy fixes issuer `react-rag-local`, audience
`react-rag-comparison-api`, a one-hour maximum token lifetime, and 30 seconds
of skew for future `iat`/`nbf` only; expiry has no grace.

Issue a short-lived local token with no network or cloud credentials:

```bash
python scripts/issue_local_access_token.py \
  --subject local-admin \
  --role admin \
  --ttl-seconds 900
```

The CLI reads the same policy and `FDIA_AUTH_SECRET` as verification and prints
the token once. Avoid putting either the secret or a resulting token directly
in a shell command: command lines and redirected output can remain in shell
history or files. Possession of the shared secret permits token issuance.
There is no revocation list; rotating the secret invalidates every outstanding
token.

This is a local shared-secret trust boundary, not passwords, signup, browser
sessions, cookies, OAuth, OIDC, SSO, an external identity provider, or
multi-user account management. Health/readiness and API documentation remain
public. The generic chat/RAG and generic JSONL answer-review routes remain
outside this bounded authentication change, and there is no frontend login
flow.

---

## Demo setup

For the strongest demo, populate `/docs` with public finance/compliance materials such as SEC 10-K or 10-Q filings, earnings call transcripts, compliance handbooks, or governance documentation. Then:

1. Run `python ingest.py` to build the Chroma vector store
2. Test at least one local-only question and one mixed local + web question
3. Verify that answers clearly separate internal corpus evidence from external web context
4. Confirm source metadata is preserved if you want to demonstrate traceability

React UI demo:

Use the two-terminal web UI commands at the top of this README. The backend
must be running before the frontend can load the document sidebar.

---

## Verified behavior

The following behaviors were manually validated in the current implementation:

- Bedrock-based agent invocation works end to end
- Local retrieval returns grounded answers for known-answer queries
- The agent reports missing information instead of fabricating answers
- Web search degrades gracefully when Tavily is not configured
- Mixed local + web questions keep external context clearly separated
- The agent does not substitute one company's filings for another

### Verified example queries

```bash
# Local corpus — 10-K filing (PDF)
python cli.py -q "What are Acme Corp's key risk factors?"

# Local corpus — compliance policy (.md)
python cli.py -q "What does the compliance policy say about blackout periods for personal trading?"

# Local corpus — research note (.txt)
python cli.py -q "What does the internal research note say about cybersecurity disclosure trends?"

# Negative test — company not in corpus
python cli.py -q "What do NVIDIA's latest filings say about key risk factors?"

# Mixed local + web
python cli.py -q "Compare Acme Corp's risk disclosures with any recent public news about cybersecurity incidents."

# Web-only fallback
python cli.py -q "What is the current Fed funds rate?"
```

---

## Tests

884 tests are collected. In the credential-free run, **872 pass, 2 optional
Tavily integration tests skip, and 10 live Bedrock tests are deselected**.
Those 10 require live Bedrock credentials plus an ingested corpus — five in `test_agent.py`
(`test_query_returns_output`, `test_query_uses_local_search`,
`test_query_with_chat_history`, `test_output_contains_result_summary`,
`test_unavailable_company_not_substituted`) and five in `test_tools.py`
(`test_local_search_returns_results`, `test_local_search_includes_source_metadata`,
`test_local_search_no_match`, `test_retriever_returns_documents`,
`test_concurrent_local_search`). The 104-test access-control suite is
credential-free and uses a deterministic test-only signing secret; it requires
no real AWS credentials, GitHub repository secret, or network. The configured
full live run passes all 884 tests. The table below
predates the comparison workflow and covers only the suites it lists. Run with:

```bash
python -m pytest tests/ -v
```

| File | Tests | What they cover |
| --- | --- | --- |
| `test_ingest.py` | 18 | Document loading (.txt, .md, .pdf), unsupported file skip, empty dir, chunking, metadata preservation, stable chunk-id derivation, idempotent re-splitting, markdown header-aware section titles, SEC `PART/ITEM` section detection, cross-page section carry-forward, table text formatting, document version/filter metadata, company/type inference precedence |
| `test_tools.py` | 13 | Local search retrieval, source metadata, metadata-filter inference, no-match handling, web search graceful degradation, markdown link output, no "date unavailable" noise, retriever documents, Chroma singleton, concurrent parallel search (thread safety), context policy admission and drop decisions reaching the report |
| `test_agent.py` | 12 | Agent build, query output, local search routing, chat history, Result Summary format, company mismatch rejection, missing-company web fallback prompt rule, source display gating, deterministic external fallback |
| `test_audit.py` | 7 | Retrieved chunk metadata parsing, enriched metadata header parsing, source extraction from tool traces, duplicate retrieved chunk suppression, JSONL audit record contents, synthetic fallback trace serialization, governance report nested in the audit record |
| `test_api.py` | 22 | Chat API returns answer metadata and audit id; governance report and contextPolicy section in the chat + streaming responses; streaming returns incremental NDJSON events; chat sources are corpus-relative (the absolute `source` path is dropped in both sync and stream paths); `SUPPORTED_DOC_SUFFIXES` derives from the loader registry and `/api/documents` lists every supported format; review routes: per-status listing with server-side ordering, cross-status detail, approve/reject with a persisted note, 404/409/422 handling, no absolute path anywhere in any response, audit join returns only `governance_report`, `wasWithheld` reads null on legacy items |
| `test_eval_runner.py` | 13 | Eval dataset validation, retrieval hit rate, grounded answer rate, unsupported claim rate, citation accuracy and citation parsing (prose "page N" and bracket "p.N" forms), claim support past the excerpt cap via full chunk text, tool-routing accuracy, fallback workflow refusal scoring, latency summary |
| `test_guardrails.py` | 10 | Bedrock-guardrail intervention detection (block message and stop_reason paths), guardrail-block eval scoring, policy ↔ provisioner constants alignment, eval-drift comparator (no-drift / metric move / case flip + latency) |
| `test_governance_report.py` | 8 | Report structure and JSON serializability, source usage counts, decision routing across outcomes, block wins over human review in decision precedence, categorical scoring for blocked answers, contextPolicy section populated with deduped drop reasons, graceful defaults on missing inputs |
| `test_review_queue.py` | 13 | enqueue writes a pending item with required fields, list_pending returns items / empty when absent, get by id or None, approve and reject move items between JSONL files, unknown-id no-op returns None, `list_items` filters by status with file-derived status tags (plus `all`, empty dir, and invalid-status ValueError), `get_any` finds items across all three files, CLI list/show/approve/reject smoke (text + json), CLI unknown-id exits 1 |
| `test_agent_review.py` | 6 | Hold/flag/wasWithheld routing driven by a genuinely ungrounded draft through the real validator and scorer (no `score_risk` monkeypatching), grounded control stays returned with sources, refusal/web-fallback answers exempt from the grounding floor, end-to-end `query()` hold landing in the pending queue with matching reason codes |
| `test_grounding_validator.py` | 5 | Runtime citation coverage and unsupported-claim rate match eval_runner on identical inputs (consistency contract), composite grounding score, empty-input handling, claims past the excerpt cap supported only through full chunk text |
| `test_risk_scorer.py` | 7 | Weighted 3-signal score, threshold bucketing into low/medium/high, YAML threshold loading with default fallback, weighted-gate boundary at exactly 0.75, mandatory grounding-floor boundary (strictly below 0.50) with explicit reason codes, floor exemption for unavailable internal answers |
| `test_context_policy.py` | 6 | YAML loading and default fallback, internal token-budget drop, low retrieval-score drop, expired-document drop (stale_document_version), draft-document drop (unapproved_document) |
| `test_agent_stream.py` | 6 | Streaming leak prevention: held and mid-block streams emit no draft content in any event (secret-phrase proof) while the queue item preserves the draft, returned streams still deliver the answer with sources, no content-bearing event precedes the validation status, audit-write and enqueue failures warn with sanitized stable codes |
| `test_api_errors.py` | 4 | Chat API error sanitization: streaming and sync failures return only the stable `code`/message/`error_id` contract (secret phrase, absolute path, and credential fragment never reach the client; full exception + correlation id in the server log), pre-first-event failure, explicit empty-message 400 preserved |
| `test_policy_validation.py` | 26 | Governance policy fail-fast: shipped YAMLs load, missing-file default fallback, invalid syntax / empty file / list root / typo'd section / non-mapping section rejected, wrong types, booleans-as-numbers, NaN/infinity, out-of-range and mis-ordered thresholds, negative budgets, truthy-string booleans, empty policy id, unknown-key forward compatibility, error messages name the field without echoing the document |
| `test_review_queue_atomic.py` | 11 | Atomic queue writes: exact-one-move with unrelated-item byte survival, barrier races (approve/reject and approve/approve) with one winner + one conflict, fsync/replace/serialization failure injection leaving originals byte-for-byte intact and temp files cleaned, the partial-move crash window + self-heal, legacy items resolvable, every line valid JSON |
| `test_review_seed.py` | 6 | Demo seed: gitignore/.gitkeep expectations, fresh dir empty, seeded items carry current-policy reason codes with no absolute paths or guardrail artifacts, real approve/reject signatures, audit-id join, rerun refusal and `--reset` confinement, no policy/config mutation |
| `test_dead_config_removed.py` | 7 | Dead-config absence + equivalence: removed flag/fields/threshold/keys are gone, PDF table extraction unaffected by the former env var, medium band derived from the two real thresholds, admission identical for all active controls, old YAMLs with removed keys still load |
| `test_document_impact.py` | 10 | load_current_chunks filters by document_id, compute_new_chunks is pure (no Chroma write), diff added/removed/modified, audit-log scan match and no-match, report shape, end-to-end modified-doc flags a past query, CLI text + json exit 0, document-id-not-found edge |
| `test_loaders_registry.py` | 6 | Built-in handlers registered, case-insensitive `handler_for`, unknown-extension returns None, register-overwrite idempotency, sorted `supported_extensions()`, PDF handler dispatch into `ingest._split_pdf` |
| `test_pii_redaction.py` | 7 | Email/SSN/phone/card redaction with per-type counts, no-PII passthrough, global-flag override, tabular-flag scoping (tabular only), both-flags-off no-op, `None`-handler safety |
| `test_loader_docx.py` | 3 | Heading-aware section load, table append into `[Extracted Tables]`, full pipeline + `filing_type` (board_minutes / committee_charter) |
| `test_loader_excel.py` | 6 | Per-sheet Documents, markdown table formatting, retrieval description prefix, tabular PII redaction default-on, PII opt-out, row-cap split with header repeat |
| `test_loader_html.py` | 5 | Section-aware split, `<script>`/`<style>` stripping, link metadata, table extraction, full pipeline + `filing_type` (regulatory_letter) |
| `test_loader_csv.py` | 6 | Markdown table + description load, row chunking with description/header repeat, tabular PII default-on, PII opt-out, `filing_type` (kyc / audit_trail), PII metadata Chroma-compatibility (Hotfix-1 regression) |
| `test_loader_pptx.py` | 3 | Per-slide Documents (including table-only slide), table extraction, full pipeline + `filing_type` (board_deck / proxy_statement / training_material) |
| `test_loader_email.py` | 3 | Header block in body, header + attachment-filename metadata, full pipeline + `filing_type` (email_archive) |
| `test_loader_json.py` | 3 | Nested jq-style key path → section_title, JSONL per-line records, full pipeline + `filing_type` (xbrl_inline / api_export) |

---

## Evaluation

The evaluation runner uses `eval/questions.jsonl` as an inspectable dataset of 32 cases covering local lookups, numeric table questions, section-specific and cross-document questions, metadata-filter phrasings, missing-company fallbacks, guardrail probes, and web-fallback questions. Each case runs the live agent against Bedrock, so a full run takes several minutes. Run:

```bash
python eval_runner.py
```

It reports:

- retrieval hit rate
- grounded answer rate
- unsupported claim rate (local-only workflows)
- citation accuracy (local-only workflows)
- tool-routing accuracy
- local-refusal accuracy (missing-company fallback workflows)
- guardrail-block accuracy (denied-topic probe cases — measures formal Bedrock guardrail enforcement, not soft prompt-level refusal)
- latency by workflow type

Claim-grounding and citation accuracy are skipped for `missing_company_fallback` cases because local retrieval is a distractor on those — there is no local ground truth to score against. Those cases are graded on whether the agent correctly admits the topic is not in the local corpus.

### Drift detection

A frozen baseline lives at [eval/baseline.json](eval/baseline.json). To check for regressions later (e.g. if AWS retunes Bedrock's topic classifier under us, or after any prompt/policy edit):

```bash
python scripts/eval_diff.py
```

This runs the eval and compares against the baseline. It prints `No drift.` when every metric is within 5 percentage points and every workflow's latency is within 30%, otherwise it prints a focused diff (which metric moved, which `case_id` flipped pass/fail, which workflow's latency shifted). To refresh the baseline after an intentional improvement: `python scripts/eval_diff.py --update-baseline`.

The current groundedness and unsupported-claim checks are deterministic and lexical — citation-filename matching and numeric-token substring support. They do not perform semantic entailment: a fabricated non-numeric claim with a well-formed citation passes them. They are intended as a stable first-pass evaluation layer, not as a replacement for human review or a larger LLM-judged eval set.

### Comparison regression suite and CI gate

The filing-comparison workflow has its own labeled regression suite, separate from the chat eval above and **credential-free** — no AWS, no Bedrock, no embeddings, no network:

```bash
python scripts/eval_comparison_regression.py            # human summary, gates enforced
python scripts/eval_comparison_regression.py --json      # machine-readable report
```

It runs five labeled fixture pairs end to end — filing registry → index → comparison record → deterministic detection → governance routing → review decision → release-gated export — from a fresh temporary registry, index, and SQLite database per fixture, twice, and scores the result against human-written labels in [tests/fixtures/comparison_regression_labels.json](tests/fixtures/comparison_regression_labels.json) (`comparison-regression.v1`).

**Regression coverage uses controlled synthetic fixtures.** Every company is fictional; the four scenario pairs are hand-written parsed-chunk fixtures in [tests/fixtures/comparison_regression/](tests/fixtures/comparison_regression/) and the standard pair is the two controlled Acme 10-K excerpts loaded through the real ingestion pipeline. The scenarios are: a standard pair (modified + added + removed + one unchanged unit), a no-change pair (zero emitted changes, no false positives), a missing-section pair (one undetermined section-level change — never mass added/removed), an ambiguous-heading pair (duplicate normalized headings stay undetermined), and a retitled-and-reworded unit (which comparison.v1 reports as an added+removed pair — the label pins that limitation honestly rather than the detector being loosened).

Ten metrics are reported with explicit numerators and denominators: change precision, change recall, change-type accuracy on matched expected changes, evidence-resolution rate, unchanged false-positive rate, undetermined-reason accuracy, fixture lifecycle accuracy, governance-decision accuracy, release-eligibility accuracy, and deterministic-result rate across repeated fresh runs. Failed fixtures, undetermined outputs, false positives, and zero-expected-change fixtures are all counted, never silently omitted; a zero-denominator rate is reported as `null` with its denominator visible and its gate passes rather than producing NaN or vanishing.

**Strict CI gates protect deterministic behavior.** Because the fixtures are controlled and the workflow is fully deterministic, every gate is exact (1.0, or 0.0 for the false-positive rate) and lives in the `GATES` constant in the runner. The suite also asserts the end-to-end workflow invariants: detection and governance idempotency, exactly one pending review per held result, pending and rejected held results refusing release, an approved held result releasing its final reviewed snapshot, detector/governance/review rows unchanged after export, and `PRAGMA integrity_check`.

`.github/workflows/comparison-regression.yml` runs it on every pull request and every push to `main` under the branch-protection check name **`comparison-regression`**, with no path filters (comparison behavior can change from the detector, validators, schemas, filing registry, ingestion section metadata, comparison store, governance policy, review logic, export logic, labels, fixtures, or the evaluator itself, so the job always runs), no repository secrets, and no AWS credentials. The only uploaded artifact is the compact metric report: metrics, denominators, gate results, and version/fixture hashes — no filing text, evidence excerpts, reviewer notes, database files, or filesystem paths.

A compact baseline lives at [eval/comparison_regression_baseline.json](eval/comparison_regression_baseline.json) — versions, per-metric values with denominators, gate configuration, and each fixture's stable detector result hash, not an opaque output snapshot. A normal run never rewrites it; it prints a human-readable diff instead. Regeneration requires `--write-baseline`, which **refuses to run in CI**, so blessing a behavior change is always an explicit human commit whose label, threshold, and metric edits appear as ordinary reviewed Git diffs. A detector or workflow version change is visible in the baseline's version block and does not automatically bless changed metrics.

**These metrics are not evidence of real-world SEC filing accuracy.** They measure whether deterministic behavior over a controlled synthetic corpus has drifted. Real public-filing evaluation — a labeled dataset drawn from actual EDGAR filings, with inter-annotator agreement and per-section metrics — remains future work.

---

## Durable API detection jobs

Authenticated initial detection is asynchronous. `POST
/api/comparisons/{comparison_id}/detect` still requires
`comparison.detect`, but it now performs only registry/source-hash validation
and one atomic SQLite enqueue. A new or equivalent active request returns
**202** with `created`, `comparisonId`, `jobId`, `jobStatus`,
`comparisonStatus`, `queuedAt`, and nullable `attemptId`; it does not open
Chroma, load Item 1A, construct a result, or run the detector. A current
already-detected comparison still returns the existing result contract with
**200** and creates no job.

The API lifecycle is `ready_for_detection → queued_for_detection → detecting
→ detected | failed`. `queued_for_detection` means one active queued job,
no running attempt, and no detector execution. A separate process claims the
job and creates the existing durable running attempt in the same transaction
that records `detection_started` and `detection_job_claimed`. It then calls the
existing deterministic detector execution seam. Success or failure is one
terminal transaction spanning the job, attempt, transition events, comparison,
and, on success, the canonical result. Direct library detection and the
comparison regression runner remain synchronous
(`ready_for_detection → detecting → detected | failed`) and create no API job.
Operator-controlled stale replay also remains synchronous and creates no
initial-detection job.

Run one queued job, or a specific queued job, with:

```bash
python scripts/run_comparison_detection_worker.py \
    --db-path comparisons/comparisons.db \
    --registry-path filing_registry/registry.jsonl \
    --worker-id local-worker-1 \
    --once

python scripts/run_comparison_detection_worker.py \
    --db-path comparisons/comparisons.db \
    --registry-path filing_registry/registry.jsonl \
    --worker-id local-worker-1 \
    --job-id djob_example \
    --once --json
```

Each invocation claims and executes at most one job; there is no polling loop.
No available job exits 0 and writes nothing. A successfully persisted detector
success or domain failure also exits 0 because the worker completed its
responsibility; invalid arguments exit 2, and storage/configuration/unexpected
worker failures exit 1 with a bounded message. The worker imports neither the
API nor authentication boundary and requires no `FDIA_AUTH_SECRET`. Its
`worker_id` is bounded local process metadata, not authenticated human
identity. Authenticated requester fields on a job come from the verified local
HS256 Principal; bearer tokens, shared secrets, arbitrary claims, and token
contents are never stored in a job or included in its idempotency hash.

Queued jobs survive an API process restart. A process death after enqueue
leaves the job queued and claimable with no attempt. A process death after
claim leaves the job and attempt `running`, the comparison `detecting`, and no
result; another worker cannot claim it in this commit. A death after the
terminal commit but before CLI output leaves the terminal state intact, and a
later worker does not repeat the detector work. There is deliberately no lease,
heartbeat, fencing generation beyond the initial claim, timeout reclaim,
automatic retry, scheduler, daemon loop, external queue, or multi-node
coordination. This local SQLite queue is a single-node reference
implementation; safe lease/reclaim behavior is the next bounded dependency.

Read-only job visibility uses
`GET /api/comparisons/{comparison_id}/detection-jobs`,
`GET /api/comparison-detection-jobs/{job_id}`, and
`GET /api/comparison-detection-jobs/{job_id}/events`, all protected by
`detection_attempt.read`. Their closed DTOs omit request and claim hashes,
token ids, result JSON, failure summaries, evidence, notes, paths, and SQL.
Job events are insert-only application records, not tamper-proof storage.

---

## Reliability visibility and operations

**Read-only, transitively.** [comparison_reliability.py](comparison_reliability.py) aggregates comparisons, detection jobs, attempts, and replays without repairing, retrying, reclaiming, replaying, deleting, or mutating anything. Its only SQLite access is one `mode=ro` snapshot read — a write is refused by the driver rather than merely avoided by convention. Replay eligibility is calculated from those already-loaded records through a **pure recovery calculation** in [detection_recovery.py](detection_recovery.py) (`build_recovery_view_from_records`, the same function the single-attempt recovery endpoint delegates to), so **no reliability API or CLI path calls `init_db`, an initializing store getter, or `recovery_view` — nothing reachable from a reliability request can create a database, create a table or index, run a migration, change database bytes or mtime, or leave a journal/WAL sidecar**, which an AST guard and poisoned-`init_db` tests pin. Filing-registry reads stay external JSONL file reads that never create or rewrite the registry. **A stale attempt reported here stays exactly as stale as it was** — direct/replay attempts still require an explicit operator replay, while a worker-owned running attempt is reported as non-reclaimable in this commit. Everything derives from local persisted records plus the checked-in recovery policy: no AWS credentials, no Bedrock, no embeddings, no vector retrieval, no network.

**An unobservable store is not an empty one.** A correctly initialized comparison database holding **zero records is a valid empty system** and produces the explicit empty report — zero counters, `null` zero-denominator rates, empty issue and failure listings, exit 0. But a **missing** database (`reliability_storage_unavailable`), an **unreadable or corrupt** one (`reliability_storage_unavailable`), and one **missing a required workflow table** — `comparisons`, `comparison_detection_jobs`, `comparison_detection_attempts`, or `comparison_detection_replays` (`reliability_data_invalid`, naming the absent table) — all **fail closed**. Reporting any of those as "zero detecting comparisons, zero failed attempts, zero unresolved issues" would be a false clean signal produced at exactly the moment the service cannot see workflow state. All three surfaces behave identically, and no partial metrics are returned. Full storage diagnostics — the configured path, the SQLite fault, which table is absent — stay **server-side only**; the API returns a stable code plus a correlation `error_id`, and the CLI prints stable codes with nothing on stdout.

```bash
python scripts/comparison_reliability_report.py                     # human summary
python scripts/comparison_reliability_report.py --issues --failures # plus listings
python scripts/comparison_reliability_report.py --json              # same contract as the API
python scripts/comparison_reliability_report.py \
    --since 2026-07-01T00:00:00+00:00 --until 2026-07-29T00:00:00+00:00
```

`--db-path` selects a database. Exit **0** for a valid report **even when unresolved issues exist** (this reports state, it does not gate on it) — including the explicit empty report for an initialized database with no records; **1** when the report is refused (`reliability_storage_unavailable` for storage that exists but cannot be read, `reliability_data_invalid` for a missing required table or inconsistent records, `reliability_dependency_unavailable` for an unanswerable filing registry), with **nothing on stdout**; **2** for invalid arguments or a `--db-path` that does not exist, since this command never creates a database. Output carries stable codes, identifiers, and counts only — never evidence, filing text, reviewer or operator notes, SQL, SQLite error text, schema DDL, absolute database paths, or exception text.

Three GET routes expose the same service: `GET /api/comparison-reliability/summary`, `.../issues`, and `.../failures`. DTOs are camelCase allowlists; `limit` is bounded (default 100, max 500) and the listings carry `total` / `returned` / `truncated` so a cap is never silent. Invalid timestamps, ranges, filters, and limits are 422 with stable codes (`invalid_timestamp`, `invalid_time_range`). Refusals are safe 500s carrying only a stable code, a concise message, and a correlation `error_id` — never a database path, SQLite error, SQL, schema text, raw exception, or any counter, gauge, issue, or partial report: `reliability_storage_unavailable`, `reliability_data_invalid`, and `reliability_dependency_unavailable` stay distinguishable so an operator can tell unreadable storage from unusable records from an unanswerable dependency. **None of the three mutates state, and the existing recovery and replay routes are unchanged.**

**Time windows.** `since` / `until` are optional, timezone-aware, and inclusive (`since <= t <= until`); a naive timestamp is rejected rather than assumed to be UTC, and `until` must not precede `since`. Historical job metrics are windowed on `queued_at`, attempt metrics on `started_at`, and replay metrics on `requested_at`. **Current-state gauges and the entire issue set are evaluated at query time and are never windowed** — "how much work is queued or stuck right now" is not a question about a past interval, which is also why `unresolvedOperationalIssues` always equals the unfiltered issue total.

**Denominators, stated exactly.** `success_rate`, `failure_rate`, and `timeout_rate` are each over `terminal_attempts` (`succeeded + failed + timed_out`); `replay_success_rate` is over terminal replay replacements. **Running work never enters a terminal denominator** — a running attempt is not a failure, and neither is a still-running replay replacement — so `attemptsRunningInWindow` is reported separately and `attemptsStarted` reconciles. A zero denominator is reported as `value: null` with its numerator, denominator, and `zeroDenominator: true` visible: never NaN, never silently 0.0, never omitted. Failure breakdowns are keyed by stable failure code, and the two version breakdowns count **failed and timed-out** attempts together.

**Job and duration metrics.** Current gauges expose queued/running/succeeded/failed detection jobs. Historical counters expose jobs queued, claimed, succeeded, and failed. Queue wait is `claimed_at − queued_at`; execution duration is `finished_at − claimed_at` for terminal claimed jobs. One percentile definition applies to jobs and attempts: **nearest-rank**, stdlib only — sort ascending, `rank = ceil(p × n)`, take index `rank − 1`. For n = 1 every percentile is the sole value; for n = 2, p50 is the lower value and p95 the upper. Attempt duration is `finished_at − started_at` over terminal attempts. A **negative** duration is excluded from statistics and counted separately — never folded into a min or mean that would then be quietly wrong.

**Operational issues** are calculated from source records, not persisted: there is no issue table and **no acknowledgement state**, so an issue keeps appearing for exactly as long as the record state producing it exists. Each record carries a closed allowlist of stable codes and identifiers — no evidence, result JSON, filing content, reviewer or operator notes, paths, SQL, or exception text — and its `recommendedActionCode` is a machine-readable code, never prose instructions. Ordering is deterministic: severity, then oldest condition first, then stable ids.

| `issueType` | Fires when | `recommendedActionCode` |
| --- | --- | --- |
| `invalid_negative_duration` | a terminal attempt finished before it started | `inspect_clock_integrity` |
| `attempt_limit_exhausted` | a `detecting` comparison has used all `max_attempts_per_comparison` attempts | `create_new_workflow_version` |
| `stale_running_attempt` | a `running` attempt reached the configured stale threshold | `inspect_and_replay_if_valid`, or `no_replay_available` when a replay would currently be refused |
| `running_detection_job_without_lease` | a worker-owned running job has crossed the conservative stale threshold; visibility only, because this commit has no lease or safe reclaim | `inspect_running_job_no_automatic_recovery` |
| `replacement_attempt_failed` | a replay's replacement attempt itself `failed` | `inspect_failure` |
| `comparison_failed` | the comparison is in `failed` | `inspect_failure` |
| `queued_detection_job` | an initial-detection job is queued and needs a one-shot worker invocation | `run_one_shot_detection_worker` |

The gauge `attemptLimitExhaustedComparisons` uses **exactly the same scope** as the `attempt_limit_exhausted` issue — `status == detecting` **and** `attempts_used >= max_attempts_per_comparison` — so the two always agree. A comparison that spent its whole attempt budget and then reached a terminal state is deliberately excluded: the `detected` one succeeded, and the `failed` one is reported as `comparison_failed`, where the attempt budget is not the operative constraint (a failed attempt is not replayable at any attempt count). Historical attempt counters are unaffected by this scope. A `timed_out` replacement is not reported as `replacement_attempt_failed`, because `timed_out` is reachable only inside a replay transaction that starts a successor — that successor's state is the live signal.

**Dependency handling is also fail-closed.** Replay eligibility is a statement about filing-registry truth, so it is the one metric here with an external dependency. When at least one attempt needs a recovery evaluation and the registry is absent, unreadable, malformed, or empty, the report is refused with the stable code `reliability_dependency_unavailable` (safe 500 + `error_id` on the API; exit 1 and no partial output on the CLI). An unanswerable question is **never** converted into `replayEligibleAttempts: 0`, an empty issue list, or a `no_replay_available` action code — that would tell an operator nothing needs action at exactly the moment the system cannot tell. When nothing needs a recovery evaluation (no stale running attempt), the count is an exact zero and the registry is not read at all; the failures listing never requires eligibility, so it never depends on the registry. Note that `filing_registry/registry.jsonl` is gitignored runtime state, so a clean clone that has not run `python ingest.py` will legitimately hit this refusal as soon as a stale attempt exists.

**Fail-closed data validation.** Metrics come from stored rows, so contradictory rows would otherwise produce clean-looking numbers that are false. The report is **refused** with the stable code `reliability_data_invalid` when it finds an incoherent job lifecycle or missing job linkage, a terminal attempt without `finished_at`, a running attempt with one, a job/attempt/comparison status outside the store's vocabulary, a replay whose source or replacement attempt is missing, or a stored timestamp that is not parseable as timezone-aware UTC. No partial metrics are returned after a structural error, on any surface. The one deliberate exception is a negative duration, which becomes an issue instead — a single skewed clock should not suppress the whole report. Detection transition *events* are deliberately not consumed: no metric in this contract needs them.

**Structured logging.** Lifecycle records cover comparison creation, durable
detection-job queue/claim/success/failure, detection attempt start and terminal
transitions, replay creation/completion, governance evaluation, authenticated
comparison-review decisions, and export creation.
Their allowlisted LogRecord fields include the existing workflow identifiers
plus Principal-derived `actor_subject`,
`actor_auth_method`, `actor_token_id`, and `required_permission` on
authenticated mutations. Job enqueue logging deliberately omits token id and
records only subject, auth method, and required permission; claim/terminal job
records carry local worker metadata and timings but no actor fields. They never
include a raw claim token or its hash, bearer token, shared secret,
arbitrary claims, Authorization header, notes, evidence, document text, paths,
SQL, environment values, or raw exception text. **These are structured fields
through stdlib logging, not guaranteed JSON**; rendering still depends on the
deployment's formatter. Records are emitted after the transaction commits and
logging failure cannot roll back committed workflow state. Idempotent requests
that change nothing emit no duplicate lifecycle record.

**What does not exist here.** No dashboard, no frontend surface, no alerting,
no notifications (email, Slack, SNS), no external monitoring integration
(Prometheus, OpenTelemetry, CloudWatch), and no scheduler, daemon/polling
worker, external queue, lease, heartbeat, automatic retry, or safe reclaim.
The only worker is an explicitly invoked one-shot local process over the
single-node SQLite queue. Reliability API reads require a verified local access token
with `reliability.read`; the credential-free local report CLI still reads the
database directly for an operator who already has filesystem access. Local
SQLite records and process logs are **application records, not tamper-proof
storage**: anyone with file access can alter them.

---

## Design decisions

### Why RAG instead of fine-tuning

For document Q&A, the knowledge base changes more often than the model itself should. RAG keeps the model fixed and injects relevant knowledge at query time, which makes it practical to update the system by re-ingesting documents instead of retraining a model.

### Why AWS Bedrock

This project uses Bedrock to keep the LLM and embedding stack inside AWS. That makes the architecture a better fit for enterprise and regulated environments where AWS-native access controls, logging, and governance matter.

### Why an agent instead of a fixed retrieval chain

A fixed retrieval chain performs one search and answers from whatever comes back. An agent gives the system a loop:

* decide what it needs
* choose a tool
* inspect the result
* search again if needed

That is useful for comparative and multi-step document questions.

### Why local vector storage first

For a portfolio project and a modest document corpus, a local persistent store keeps the system simple. The architecture can later be swapped to a managed vector database if scale requires it.

---

## Supported Formats

The pipeline ingests 10 formats (14 extensions) through a `FormatHandler` registry. PII redaction follows a two-tier default: CSV and XLSX redact at load time because their content is structurally identifier-heavy (account numbers, user IDs, and emails populated per row). Every other format respects the global `PII_REDACT_AT_INGEST` flag, which is off by default to keep citations faithful to the source text.

| Format | Extensions | Library | Tables | PII default | Notable filing_type patterns |
| --- | --- | --- | --- | --- | --- |
| PDF | `.pdf` | PyMuPDF | Yes | Global flag | `10-k`, `10-q`, `8-k` |
| Markdown | `.md` | LangChain MarkdownHeaderTextSplitter | No | Global flag | `policy` |
| Text | `.txt` | stdlib | No | Global flag | `research_note` |
| DOCX | `.docx` | python-docx | Yes | Global flag | `board_minutes`, `committee_charter` |
| XLSX | `.xlsx`, `.xls` | openpyxl | Native tabular | Tabular flag ON | `control_matrix`, `risk_register` |
| HTML | `.html`, `.htm` | BeautifulSoup4 + HTMLHeaderTextSplitter | Yes | Global flag | `regulatory_letter` |
| CSV | `.csv` | stdlib csv | Native tabular | Tabular flag ON | `audit_trail`, `kyc` |
| PPTX | `.pptx` | python-pptx | Yes | Global flag | `board_deck`, `proxy_statement`, `training_material` |
| EML / MSG | `.eml`, `.msg` | stdlib email + extract-msg | Attachment filenames only | Global flag | `email_archive`, `regulatory_letter` |
| JSON / JSONL | `.json`, `.jsonl` | stdlib json | Native structured | Global flag only | `xbrl_inline`, `api_export` |

The `10-k` / `10-q` / `8-k` / `policy` / `research_note` patterns are inferred from filename and text, so they classify any format, not just the row they appear on.

---

## Retrieval strategy

### Current implementation

* Fixed-size chunking with overlap
* Top-k retrieval from the local vector store
* Agent-based tool selection between local retrieval and web search
* Section-aware chunking for markdown and 10-K-style PDF sections
* PDF table extraction into retrievable page text when PyMuPDF detects tables
* Metadata-aware retrieval filters for explicit company, filing type, and year queries

### Recommended next step

For long financial documents, hybrid retrieval or a reranker is the next quality
step. Exact strings such as filing names, item numbers, dollar amounts, and
dates often deserve keyword matching in addition to dense embeddings.

### Metadata

For auditability-oriented workflows, each chunk should preserve:

* source file
* page number for PDFs
* optional section heading
* company, document type / filing type, and year when available
* document ID, content hash, and version hash for version traceability

---

## Current limitations

This project is not yet a production-grade compliance system.

Known limitations:

* It is not legal advice or compliance advice
* Answer quality depends on retrieval quality
* Table-heavy PDFs may require better parsing
* Page-number citation support depends on ingestion metadata
* Web results are supplemental and need clear labeling
* The evaluation pipeline is lightweight and rule-based; a larger benchmark set would be needed for production confidence
* There are no production-grade approval workflows or document-level permissions in the current build
* Authentication and permission-based authorization cover the comparison workflow only. They do not cover the generic chat/RAG API or its JSONL answer-review queue, and they add no assignment or automatic delivery. New replay and review actions created through the protected comparison API derive their actors from a cryptographically verified local Principal; caller-supplied actor identifiers cannot override it. Direct library callers retain the explicit `legacy_self_asserted` compatibility path, and rows created before this boundary remain readable with that same marker rather than being retroactively treated as authenticated. Decision events remain application records in local SQLite, not tamper-proof audit storage
* The runtime grounding validator is deterministic and lexical (citation-string and numeric-token matching); it is not semantic entailment validation
* Filing comparison runs end to end, but **only for Item 1A Risk Factors and only proven on controlled synthetic fixtures**. It remains deterministic heading-based extraction and exact-match alignment with no semantic similarity, embeddings, or LLM comparison; unprovable changes become `undetermined`. Its lexical/numeric validators are not semantic entailment, and comparison risk measures output trust and review necessity rather than financial materiality or legal/investment risk. Comparison reviews now require `review.decide`; approve/reject identity is derived from the verified Principal while reason, note, edit-validation, lifecycle, and export-eligibility semantics remain unchanged. Export creation requires `export.create`, but artifacts remain persisted local JSON only: no delivery, email, external storage, frontend review editor, signature, or tamper-proof store. Authentication metadata does not enter `comparison.v1` or export payload hashes. To preserve the existing `comparison.export.v1` contract and hashes, that artifact still carries its legacy `reviewer_id_basis=self_asserted_local_metadata` field and does not carry the workflow record's authenticated provenance; representing that provenance in a portable export requires an explicitly versioned future export contract
* The comparison regression suite and its `comparison-regression` CI gate protect deterministic behavior over **five controlled synthetic fixture pairs** (fictional companies, hand-written chunk fixtures plus the two Acme excerpts). Exact gates there mean "nothing drifted", **not** "the detector is accurate": no metric in this repository has been measured against real SEC filings, and real public-filing evaluation with a labeled EDGAR dataset remains future work. The gated limitations remain Item 1A only, lexical heading alignment with no similarity stage, local SQLite storage, and a local shared-secret authentication boundary
* The filing registry ([filing_registry.py](filing_registry.py)) detects exact-content duplicates (a renamed byte-identical file records a `duplicate` outcome and is not indexed twice) and identity conflicts (same company/form/period from different content records a `conflict` and is not indexed). Detection happens at ingest time within the local registry only — there is no fuzzy near-duplicate detection, and cross-process registry writes are unprotected (same JSONL limitation as the review queue)
* The review queue is local JSONL: atomic per file and race-safe in one process, but the two-file move is not a transaction and cross-process access is unprotected
* Authenticated initial comparison detection has a durable SQLite job and a separate one-shot worker, but this is a **single-node reference queue**, not a distributed or highly available execution system. There is no lease, heartbeat, lease expiry, timeout reclaim, fencing generation beyond the initial claim, automatic retry/replay, dead-letter queue, scheduler, daemon loop, backoff, or external queue. A queued job survives API restart and can be claimed later; a worker crash after claim leaves the job and attempt `running` and the comparison `detecting` indefinitely. Another worker cannot reclaim that work in this commit. The reliability view reports the condition but never repairs it. Direct library/regression detection and operator-controlled stale replay remain synchronous; a worker-owned claim is deliberately not eligible for stale replay because that would bypass the missing fencing/reclaim protocol. The generic chat side still has no durable task record, and the audit log remains an append-only local file, not immutable storage
* Replay through the protected API requires `recovery.replay`, and every API-created replay stores the verified Principal subject with `local_hs256` attribution; an optional deprecated `operatorId` is accepted only when it exactly matches that subject. Direct library calls and historical rows stay marked `legacy_self_asserted`. Replay records and detection transition events are append-only **application records** in a local SQLite file, not tamper-proof storage
* Detection attempts and their transition events are append-only **application records** in a local SQLite file — not tamper-proof storage. Anyone with file access can alter them
* Reliability visibility is **read-only and exposes state only**: it never repairs, retries, reclaims, replays, deletes, or mutates a workflow record. Its API routes require `reliability.read`; the local report CLI remains available to an operator with filesystem access. There is still no dashboard, frontend, alerting, notification, external monitoring integration, scheduler, daemon loop, lease manager, or external queue. Lifecycle fields travel through stdlib logging but are not guaranteed JSON. Replay eligibility still depends on the filing registry and fails closed when it cannot be evaluated. Local SQLite records and process logs remain application records, not tamper-proof storage

---

## What would make this more production-like

**Maturity, stated plainly: this is a Stage 3 controlled review workflow — local, over a controlled synthetic corpus, not an external pilot, not production, and not real-filing benchmarked.** The Stage 2 governed Q&A loop (evidence, validation, risk routing, local human review) plus a complete executable filing-comparison loop: a persisted workflow (create → detect → govern → review → release-gated export) over durable filing identities, structured changes carrying two-sided chunk-resolvable evidence, attributable terminal review decisions, and deterministic export artifacts — all pinned by a labeled regression suite that runs as a strict CI gate on every pull request with no credentials.

What that qualification means concretely: the corpus is five **controlled
synthetic** fixture pairs (fictional companies), so precision / recall /
change-type accuracy / evidence resolution / governance and release accuracy
of 1.0 measure **deterministic-behavior stability, not accuracy on real
filings** — nothing here has been measured against actual EDGAR documents.
Comparison routes now have a local signed-token and exact-permission boundary,
but it is not OAuth, OIDC, SSO, enterprise identity, account management, or
document-level authorization. Audit logs, review queues, comparison records,
and export artifacts are local files and SQLite, not immutable or tamper-proof
storage. This is not enterprise-ready, not compliance-grade, and carries no
production security posture.

**Stage 3.5 work is in progress and not complete.** Five bounded steps have
landed (roadmap step 12): durable detection attempts and lifecycle events;
explicit bounded stale replay; read-only reliability metrics and correlated
structured logs; and now signed local access tokens, policy-derived route
permissions, plus authenticated attribution for new protected-API replay and
review actions; followed by durable authenticated initial-detection jobs and a
credential-free one-shot worker.
The authentication step proves callers inside one local shared-secret trust
boundary; possession of that secret permits token issuance, revocation is not
implemented, and rotation invalidates every outstanding token. It does not
make SQLite or logs tamper-proof and does not protect the generic chat/RAG
surface.

The queue is local SQLite and the worker is explicitly invoked, one job at a
time. What still does not exist is safe crash recovery: no lease, heartbeat,
lease expiry, fencing generation, timeout takeover, reclaim, automatic retry,
scheduler, daemon loop, or external queue. A worker crash after claim leaves
the job and attempt `running`; even an authorized human cannot safely replay
that worker-owned attempt in this commit. Maturity therefore remains **Stage
3**. Stage 3.5 remains in progress; the next dependency is **lease, heartbeat,
fencing, and safe reclaim**.

### Next: the financial filing comparison workflow (current roadmap)

The next implementation phase is a governed two-filing change review for one company, built in this order (each step depends on the ones before it):

1. Comparison output schema (structured, versioned — defined before any UI) — **done as a contract**: `comparison.v1` in [governance/comparison_schema.py](governance/comparison_schema.py), machine-validated (Pydantic, cross-filing and evidence-side invariants) with golden fixtures in `tests/`
2. Durable filing identity — **done**: [filing_registry.py](filing_registry.py) + [corpus_manifest.yaml](corpus_manifest.yaml). Explicit manifest metadata (company, form type, `period_end`, `filing_date` — never inferred from filenames or mtimes) mints a per-filing `filing_id` (`company:form:period_end`), distinct from the year-stripped `document_family_id` that successive filings share (kept, with `document_id` as its legacy alias, for the impact workflow). The registry records one durable outcome per attempted source (`parsed` / `duplicate` / `failed` / `conflict`, atomic JSONL writes), orders same-company/same-form filings chronologically by `period_end`, and builds validated `FilingReference` sides via `to_filing_reference`. Two controlled fictional Acme 10-K excerpts (FY2024 + FY2025, distinct periods, shared family) are the demo dataset — they prove identity and ordering, not comparison quality, and structured comparison itself still does not exist. **The next dependency is step 3 (persistent comparison entity)**
3. Persistent comparison entity (create, list, reopen across restarts) — **done**: [comparison_store.py](comparison_store.py) (stdlib SQLite, no ORM — chosen over JSONL because comparisons are mutable workflow records that later commits transition and attach children to) plus `POST /api/comparisons`, `GET /api/comparisons/{id}`, and `GET /api/comparisons` (filters: `filing_id`, `status` — including `queued_for_detection` and `detecting`). Creation validates the pair against the filing registry (existence, parsed status, undisputed identity, complete metadata, same company/form, strictly increasing `period_end`, supported section scope) and returns stable 422 reason codes; identical requests are idempotent (deterministic `cmp_` id + unique logical-key constraint, `created` flag, no duplicate rows under concurrent creates). Records persist in an explicit **`ready_for_detection`** state: no changes, evidence, validation, risk, or review fields exist on them — a pre-detection record cannot masquerade as a completed comparison. **The next dependency is step 4 (Item 1A section alignment and structured change detection)**
4. Item 1A (Risk Factors) section alignment between the two filings — **done**: ingestion stamps a canonical `section_key` (`item_1a_risk_factors`, mapped from splitter-produced headings only — body text can never classify) plus a `chunk_seq` ordering; [comparison_detector.py](comparison_detector.py) loads complete sections by `filing_id` + `section_key` via metadata `get` (no vector retrieval anywhere in the path) with an explicit completeness proof (registry chunk_count vs indexed count)
5. Structured changes: added / removed / modified / undetermined — **done** with deterministic rules only (exact normalized heading match, then exact content match; deliberately no similarity stage in v1). "Removed" is never inferred from a retrieval miss: added/removed are emitted only when both sections are proven complete, and anything unprovable or ambiguous becomes `undetermined` with a stable reason code
6. Previous-filing and current-filing evidence attached to every change — **done**: every `EvidenceReference` resolves back to an indexed chunk (chunk_id, page, section, excerpt from the indexed text), with the comparison.v1 side rules enforced by schema validation over detector output
7. Comparison-specific deterministic validation — **done**: all six checks compute per change. `evidence_presence` / `entity_consistency` / `period_consistency` (structural), plus `citation_support` (summary heading supported by resolved evidence on every required side; references must resolve to the correct filing's indexed chunks; undetermined reasons must match the actual evidence shape), `numeric_consistency` (summary numeric tokens matched full-token against the required side under deterministic normalization — commas, `$`, `%`, signs; "21" can never match "210"; `from X to Y` pins previous/current attribution; ambiguous attribution fails), and `direction_consistency` (narrow vocabulary — increased/decreased/unchanged plus the added/removed structural markers; directional claims require verified value ordering; inverted directions fail). All three ([comparison_validators.py](comparison_validators.py)) prefer `not_applicable` or explicit failure over hollow passes, with stable reason codes and validator versions in every check. Detector is now `item1a_detector.v2` / `comparison_workflow.v2`: results stored under v1 remain readable as old-version output, and re-detection happens as a new comparison — a version change is a `detector_version_superseded` 409, deliberately distinct from `comparison_inputs_stale`
8. Reachable review routing for comparison results — **done**: a dedicated comparison risk policy ([policies/comparison_risk_policy.yaml](policies/comparison_risk_policy.yaml), fail-fast validated like the other governance YAML) scores **output trust and review necessity only** (failed-validation rate, undetermined-change rate, evidence-failure rate — explicit denominators, `not_applicable` never counts, change counts deliberately excluded as a materiality proxy). Mandatory-review rules (any failed check by name, any undetermined change, any unexpected `not_run`) hold a comparison independently of the weighted score. Evaluations persist immutably in SQLite keyed by (comparison, result hash, policy id+version) — idempotent, concurrent-safe, old policy versions stay readable — with a validated governed comparison.v1 snapshot updating only risk and review; a held decision creates exactly one pending `comparison_review_items` row in the same transaction. `POST/GET /api/comparisons/{id}/governance` + `GET /api/comparison-reviews`. Vocabulary honesty: `returned_with_warning` is reachable only if an operator relaxes the mandatory rules, and `blocked` is not produced by policy v1. The controlled clean Acme result returns; held paths are proven with synthetic results
9. Reviewer attribution, required decision reasons, and draft-edit support — **done** ([comparison_review.py](comparison_review.py)): one terminal approve/reject per pending comparison review, recorded as an append-only SQLite decision event with an action-specific allowlisted reason code (edits ⇔ `approved_with_summary_edits`), a required bounded note, both result hashes, and the complete final reviewed comparison.v1 snapshot. New API decisions require `review.decide` and derive the reviewer id from the verified Principal; the deprecated request field cannot override it. Existing rows remain readable as `legacy_self_asserted`. Editing remains summary-only by change_id and is revalidated against original evidence; rejection, idempotency, concurrency, and transactional semantics are unchanged. `GET/POST /api/comparison-reviews/{id}[...]` + `/events`
10. Release-gated structured export (formerly phrased "approved-only export") — **done** ([comparison_export.py](comparison_export.py), contract in [governance/comparison_export_schema.py](governance/comparison_export_schema.py)): a persisted, deterministic `comparison.export.v1` JSON artifact created only when the persisted governance workflow already released the result. Eligibility is resolved from stored records, never client-submitted state: `returned` and `returned_with_warning` evaluations export their governed snapshot directly, held evaluations export the final reviewed snapshot from their approved terminal review event, and pending, rejected, and blocked outcomes refuse with stable reason codes. Export creation now requires `export.create` and reads require `export.read`; release eligibility, payload construction, and payload hashing are unchanged, and no new token or policy metadata is inserted into the export payload. For version/hash compatibility, `comparison.export.v1` retains its legacy `reviewer_id_basis=self_asserted_local_metadata` field and omits the workflow record's authenticated provenance; carrying that provenance requires a future versioned export contract. Export rows remain application records, **not signed or tamper-proof storage**. `POST/GET /api/comparisons/{id}/exports` + `GET /api/comparison-exports/{export_id}` — persisted JSON only: no delivery, downloads, webhooks, or frontend
11. A labeled comparison dataset with precision/recall metrics and CI regression gates — **done** ([scripts/eval_comparison_regression.py](scripts/eval_comparison_regression.py), labels in [tests/fixtures/comparison_regression_labels.json](tests/fixtures/comparison_regression_labels.json), fixtures in [tests/fixtures/comparison_regression/](tests/fixtures/comparison_regression/), CI in [.github/workflows/comparison-regression.yml](.github/workflows/comparison-regression.yml)): a versioned human-readable label contract (`comparison-regression.v1`) over five **controlled synthetic** fixture pairs — standard (modified/added/removed/unchanged), no-change, missing-section, ambiguous-heading, and retitled-and-reworded — each run end to end from fresh temporary state, twice, with ten metrics reported at explicit denominators and exact CI gates. The suite also pins the workflow invariants (idempotency, one pending review per held result, pending/rejected refusing release, approved releasing the reviewed snapshot, snapshot preservation after export, SQLite integrity). Branch-protection check name `comparison-regression`; credential-free (no AWS, no Bedrock, no network); baseline regeneration requires `--write-baseline` and is refused in CI. **Coverage is controlled and synthetic — the metrics measure deterministic-behavior stability, not real-filing accuracy. Real public-filing evaluation remains future work.** The next dependency is step 12
12. Then Stage-3.5 reliability and authentication work (state machine, retries, durable failure handling, auth) — **in progress, not complete**. The first reliability step is done: comparison detection has **durable execution attempts and an explicit lifecycle**. Both the direct synchronous path (`ready_for_detection → detecting → detected | failed`) and the asynchronous worker path (`queued_for_detection → detecting → detected | failed`) create the same attempt record and append-only `detection_started` / terminal event before computation. Attempt ids, monotonic attempt numbers, versions, source hashes, timestamps, result hash or stable safe failure fields remain transactionally coherent with the comparison and result. The authenticated API no longer starts an attempt inline; its attempt exists only after a worker claims the durable job. Attempts and events remain readable at `GET /api/comparisons/{id}/detection-attempts`, `GET /api/detection-attempts/{id}`, and `GET /api/detection-attempts/{id}/events`. **They are append-only application records, not tamper-proof storage.**

    The second reliability step is also done: **bounded stale-detection recognition and operator-controlled replay** ([detection_recovery.py](detection_recovery.py), policy in [policies/detection_recovery_policy.yaml](policies/detection_recovery_policy.yaml)). Staleness remains eligibility, never an automatic action: the read-only recovery view mutates nothing, and only an explicit replay transaction retires an eligible direct/replay attempt, starts its bounded replacement, and records the linkage. Protected-API replay requires `recovery.replay`; its actor and idempotency hash use the verified Principal subject, never a token or caller-supplied identity. Direct library callers retain the explicit legacy attribution path. Replay remains synchronous and creates no initial-detection job. A worker-owned running attempt is refused until leases and fencing make reclaim safe

    The third reliability step is also done: **read-only operator visibility, structured reliability metrics, and correlated transition logging** ([comparison_reliability.py](comparison_reliability.py), CLI in [scripts/comparison_reliability_report.py](scripts/comparison_reliability_report.py), full contract in [Reliability visibility and operations](#reliability-visibility-and-operations)). The APIs require `reliability.read`; the credential-free local CLI remains a direct operator tool. Read-only SQLite behavior, exact denominators, fail-closed validation, issue calculation, and post-commit logging semantics remain unchanged

    The fourth bounded Stage-3.5 step is done: **authenticated comparison-workflow actors and permission-based authorization**. A fail-fast checked-in policy defines local HS256 token verification, allowlisted roles, and exact permissions. Every comparison route declares its permission; authentication and authorization happen before resource lookup or mutation. New protected-API replay, review, and detection-job requester records derive actor identity from the immutable verified Principal; direct library calls and historical replay/review rows retain their explicit legacy attribution. This is deliberately local shared-secret authentication, not OAuth/OIDC/SSO or account management

    The fifth bounded Stage-3.5 step is now done: **durable asynchronous authenticated initial-detection jobs and one-shot worker execution**. API detect atomically enqueues and returns 202 without detector/Chroma work; a separate credential-free invocation claims at most one job, creates the existing running attempt, executes the existing deterministic seam, and atomically finalizes job, attempt, events, result, and comparison. Equivalent enqueues and claim races are storage-constrained and idempotent. Queued and claimed state survives process restart. This is still a local single-node reference queue: worker identity is process metadata, there is no scheduler or daemon, and a crash after claim is stuck because no lease, heartbeat, fencing, or safe reclaim exists. **Stage 3.5 remains in progress; the next dependency is lease, heartbeat, fencing, and safe reclaim**

Explicitly deferred until after that path: document-level permissions (formerly "Phase 6"), broader authentication for generic chat/RAG, new document formats, OCR, rerankers and vector-store migration, further generic chat UI redesign, additional generic chat eval expansion, LLM-as-judge evaluation, NIST documentation expansion, and Gradio parity.

### Foundations already in place

Checked items are partially addressed in the current build. Unchecked items are logical next steps:

### 1. Stronger auditability

* [x] Return file name and page number in agent answers (available when ingestion metadata includes them)
* [x] Separate internal evidence from external web evidence in agent output
* [x] Expose retrieved chunk metadata directly in the answer view
* [x] Add audit logs for queries, retrieved sources, and response traces

### 2. Evaluation

* [x] Retrieval hit rate
* [x] Grounded answer rate
* [x] Unsupported claim rate
* [x] Citation accuracy
* [x] Tool-routing accuracy (verified via tests: correct tool for local vs web vs missing company)
* [x] Latency by workflow type

### 3. Better document handling

* [x] Multi-format ingestion (10 formats / 14 extensions via FormatHandler registry: PDF/MD/TXT + DOCX/XLSX/HTML/CSV + PPTX/EML/JSON)
* [x] Section-aware chunking for filings and manuals (markdown headers; SEC `PART`/`ITEM` boundaries for 10-Ks)
* [x] Improved table extraction
* [x] Document version metadata (per-chunk content hash + version hash; no lifecycle management yet)
* [x] Durable filing identity ([filing_registry.py](filing_registry.py) + [corpus_manifest.yaml](corpus_manifest.yaml)): explicit per-filing `filing_id` distinct from the shared `document_family_id`, with `period_end`/`filing_date` taken only from declared manifest metadata — never inferred from filenames or mtimes
* [x] Chunk-level idempotent re-ingestion (stable content-hash chunk IDs upsert instead of duplicating), plus registry-level exact-duplicate detection — a byte-identical file under a second name records a `duplicate` outcome and is not indexed twice
* [x] Ingestion outcome manifest: every attempted source records `parsed` / `duplicate` / `failed` / `conflict` in the filing registry (atomic JSONL), including failed loads with a safe error code
* [x] Metadata filters by company, filing type, and year
* [x] Document change impact analysis CLI (`scripts/document_impact.py`) — given a document change, reports which past queries used now-stale chunks via excerpt-fingerprint matching against the audit log; family-scoped (`--document-id`) with an explicit refusal when a family spans multiple filings, or filing-scoped (`--filing-id`)

After changing document-handling code or adding new corpus files, rerun
`python ingest.py` so Chroma receives the latest table text and metadata.
Re-ingestion is also what stamps the new filing-identity metadata
(`filing_id`, `document_family_id`, `period_end`, `filing_date`) onto chunks
and populates `filing_registry/registry.jsonl` — a store ingested before this
feature simply lacks those fields until you re-run ingestion (existing
filters and answers keep working in the meantime).

### 4. Safer workflow controls

* [x] Bedrock Guardrails wired end-to-end (denied topics, PII anonymization, contextual grounding) — policy in [policies/guardrails-policy.md](policies/guardrails-policy.md), provisioner at [scripts/setup_guardrail.py](scripts/setup_guardrail.py); the operator runs the provisioner once per environment and sets `BEDROCK_GUARDRAIL_ID` / `BEDROCK_GUARDRAIL_VERSION`
* [x] Governance report per answer (audit ID, validation, risk, decision visible in API + React + CLI)
* [x] Context policy admission with token budgets and drop-reason tracking
* [x] Runtime grounding validation (citation coverage, grounding score, unsupported claims)
* [x] Human review queue for high-risk answers (hold or flag mode via `HUMAN_REVIEW_HOLD`, CLI approve/reject via `scripts/review_queue.py`)
* [x] Stricter policy around when web research is allowed (agent prompt enforces local-first)
* [~] Prompt-level uncertainty statements (the agent is instructed to state when data is unavailable or weak; no measured confidence signal is captured or validated)
* [x] Better handling for low-signal retrieval results (agent refuses to substitute wrong company data)

### 5. Enterprise-oriented deployment path

* [ ] Hosted vector store
* [ ] API service layer
* [x] Local comparison-workflow authentication and exact permission checks (bounded shared-secret reference implementation; generic chat/RAG and document-level authorization remain out of scope)
* [ ] Document-level permissions
* [ ] IAM-based service access
* [~] Logging and monitoring — the comparison workflow has permission-protected read-only reliability metrics, issue/failure listings, a local CLI ([scripts/comparison_reliability_report.py](scripts/comparison_reliability_report.py)), and correlated structured lifecycle logs with authenticated actor context on mutations. **That is local visibility, not monitoring**: no dashboard, alerting, notifications, external monitoring backend (Prometheus/OpenTelemetry/CloudWatch), guaranteed-JSON format, or log shipping. The generic chat side still has no durable task record to report on at all
* [x] Human review workflow for sensitive outputs (portfolio-minimal version: hold/flag queue, CLI approve/reject)

---

## Future direction

This project can be extended toward:

* compliance copilot workflows
* filing analysis assistants
* internal policy search
* investment research support tools
* auditable enterprise knowledge agents for regulated industries
