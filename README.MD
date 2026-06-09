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
- [x] Streams React answer tokens as the Bedrock/LangGraph response is generated
- [x] Caps Gradio chat history to prevent stale formatting from older turns
- [x] Exposes retrieved chunk metadata in the React answer view
- [x] Extracts PDF table text into retrievable chunks when PyMuPDF detects tables
- [x] Tracks document version metadata and supports retrieval filters by company, filing type, and year
- [x] Writes JSONL audit logs for queries, retrieved sources, and response traces
- [x] Includes a deterministic evaluation runner for retrieval, citations, grounding, unsupported claims, routing, and latency
- [x] Wraps the agent in a governance layer: context policy admission, runtime grounding validation, per-answer governance report, and document change impact analysis
- [x] Holds high-risk answers in a human review queue (hold or flag mode via `HUMAN_REVIEW_HOLD`) with a CLI approve/reject flow
- [x] 162 tests covering multi-format ingestion, per-format loaders, PII redaction, tools, agent behavior, auditability, API responses, evaluation metrics, governance reporting, context policy admission, runtime grounding validation, document change impact analysis, the human review queue, and thread safety

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

The project wraps the document agent in a governance layer. It controls what information enters the model, validates whether each answer is grounded in retrieved evidence, scores risk, and produces an audit-ready report for every answer. The report is visible in the API response, the React panel, and a one-line CLI summary, and the full record is written to the JSONL audit log.

The point is to make each answer defensible: you can see which chunks were admitted or dropped and why, how well the answer is grounded, what the risk decision was, and which past answers depend on a document that just changed.

Currently supports:

- governance report per answer (audit ID, source usage, validation, risk, decision)
- context policy admission with drop reasons and token budgets
- runtime grounding validation (citation coverage, grounding score, unsupported claims)
- categorical block scoring (blocked answers report risk=high, validation=N/A)
- document change impact analysis CLI (`scripts/document_impact.py`)
- human review queue for high-risk answers: decision routing with a `held_for_review` outcome, hold or flag mode gated by `HUMAN_REVIEW_HOLD`, CLI approve/reject via `scripts/review_queue.py`
- internal-first retrieval policy
- external context separation
- document version and chunk traceability
- Bedrock Guardrails for denied topics and PII handling
- JSONL audit logs for query and retrieval traces
- deterministic evaluation for grounding, citations, routing, and drift

Planned extensions:

- document-level permissions (Phase 6)

The governance modules live in [governance/](governance/); the design doc is [Governance_layer.md](Governance_layer.md) and the build log is [GOVERNANCE_PROGRESS.md](GOVERNANCE_PROGRESS.md).

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
├── frontend/        # React + TypeScript + Tailwind chat UI
├── docs/            # sample docs (.md, .txt, .pdf) — corpus only; never indexed: policies/
├── policies/        # safety policy docs (e.g. guardrails-policy.md) — outside corpus
├── scripts/         # operator scripts (setup_guardrail.py, eval_diff.py, document_impact.py, review_queue.py)
├── chroma_db/       # persistent vector store
├── governance/      # governance layer (report, grounding, risk, context policy, impact, review queue)
│   ├── governance_report.py  # per-answer report assembly
│   ├── grounding_validator.py # runtime citation + claim grounding
│   ├── risk_scorer.py        # 3-signal risk score + categorical block override
│   ├── context_policy.py     # chunk admission, token budgets, drop reasons
│   ├── impact.py             # document change diff + audit log scan
│   └── review_queue.py       # file-backed human review queue (enqueue/list/approve/reject)
├── tests/           # 162 tests (ingest, per-format loaders, registry, PII, tools, agent, auditability, API, eval, guardrails, governance, human review queue, thread safety)
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
| `PII_REDACT_AT_INGEST`  | No       | Global PII redaction on load. Default `false`            |
| `PII_REDACT_TABULAR_AT_INGEST` | No | Tabular-only PII redaction for CSV/XLSX. Default `true`  |
| `INGEST_TABLE_EXTRACTION` | No     | Toggle PDF/DOCX/HTML/PPTX table extraction. Default `true` |

\* In deployed environments, IAM roles are the preferred access pattern for Bedrock and related AWS services.

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

162 tests, all passing. Run with:

```bash
python -m pytest tests/ -v
```

| File | Tests | What they cover |
| --- | --- | --- |
| `test_ingest.py` | 18 | Document loading (.txt, .md, .pdf), unsupported file skip, empty dir, chunking, metadata preservation, stable chunk-id derivation, idempotent re-splitting, markdown header-aware section titles, SEC `PART/ITEM` section detection, cross-page section carry-forward, table text formatting, document version/filter metadata, company/type inference precedence |
| `test_tools.py` | 13 | Local search retrieval, source metadata, metadata-filter inference, no-match handling, web search graceful degradation, markdown link output, no "date unavailable" noise, retriever documents, Chroma singleton, concurrent parallel search (thread safety), context policy admission and drop decisions reaching the report |
| `test_agent.py` | 12 | Agent build, query output, local search routing, chat history, Result Summary format, company mismatch rejection, missing-company web fallback prompt rule, source display gating, deterministic external fallback |
| `test_audit.py` | 7 | Retrieved chunk metadata parsing, enriched metadata header parsing, source extraction from tool traces, duplicate retrieved chunk suppression, JSONL audit record contents, synthetic fallback trace serialization, governance report nested in the audit record |
| `test_api.py` | 8 | Chat API returns answer metadata and audit id; governance report and contextPolicy section in the chat + streaming responses; streaming returns incremental NDJSON events; `SUPPORTED_DOC_SUFFIXES` derives from the loader registry and `/api/documents` lists every supported format |
| `test_eval_runner.py` | 10 | Eval dataset validation, retrieval hit rate, grounded answer rate, unsupported claim rate, citation accuracy, tool-routing accuracy, fallback workflow refusal scoring, latency summary |
| `test_guardrails.py` | 10 | Bedrock-guardrail intervention detection (block message and stop_reason paths), guardrail-block eval scoring, policy ↔ provisioner constants alignment, eval-drift comparator (no-drift / metric move / case flip + latency) |
| `test_governance_report.py` | 7 | Report structure and JSON serializability, source usage counts, decision routing across outcomes, block wins over human review in decision precedence, categorical scoring for blocked answers, contextPolicy section populated with deduped drop reasons, graceful defaults on missing inputs |
| `test_review_queue.py` | 9 | enqueue writes a pending item with required fields, list_pending returns items / empty when absent, get by id or None, approve and reject move items between JSONL files, unknown-id no-op returns None, CLI list/show/approve/reject smoke (text + json), CLI unknown-id exits 1 |
| `test_agent_review.py` | 2 | finalize hold mode (answer replaced with the held notice, draft queued + auditable), flag mode (answer returned unchanged, item still enqueued) |
| `test_grounding_validator.py` | 4 | Runtime citation coverage and unsupported-claim rate match eval_runner on identical inputs (consistency contract), composite grounding score, empty-input handling |
| `test_risk_scorer.py` | 4 | Weighted 3-signal score, threshold bucketing into low/medium/high, YAML threshold loading with default fallback, human-review trigger |
| `test_context_policy.py` | 6 | YAML loading and default fallback, internal token-budget drop, low retrieval-score drop, expired-document drop (stale_document_version), draft-document drop (unapproved_document) |
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

The evaluation runner uses `eval/questions.jsonl` as a small, inspectable dataset. Run:

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

The current groundedness and unsupported-claim checks are deterministic and rule-based. They are intended as a stable first-pass evaluation layer, not as a replacement for human review or a larger LLM-judged eval set.

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

---

## What would make this more production-like

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
* [x] Document versioning
* [x] Deduplication across repeated filings (stable content-hash chunk IDs + Chroma upsert)
* [x] Metadata filters by company, filing type, and year
* [x] Document change impact analysis CLI (`scripts/document_impact.py`) — given a document change, reports which past queries used now-stale chunks via excerpt-fingerprint matching against the audit log

After changing document-handling code or adding new corpus files, rerun
`python ingest.py` so Chroma receives the latest table text and metadata.

### 4. Safer workflow controls

* [x] Bedrock Guardrails wired end-to-end (denied topics, PII anonymization, contextual grounding) — policy in [policies/guardrails-policy.md](policies/guardrails-policy.md), provisioner at [scripts/setup_guardrail.py](scripts/setup_guardrail.py); the operator runs the provisioner once per environment and sets `BEDROCK_GUARDRAIL_ID` / `BEDROCK_GUARDRAIL_VERSION`
* [x] Governance report per answer (audit ID, validation, risk, decision visible in API + React + CLI)
* [x] Context policy admission with token budgets and drop-reason tracking
* [x] Runtime grounding validation (citation coverage, grounding score, unsupported claims)
* [x] Human review queue for high-risk answers (hold or flag mode via `HUMAN_REVIEW_HOLD`, CLI approve/reject via `scripts/review_queue.py`)
* [x] Stricter policy around when web research is allowed (agent prompt enforces local-first)
* [x] Clearer confidence and uncertainty handling (agent states when data is unavailable or weak)
* [x] Better handling for low-signal retrieval results (agent refuses to substitute wrong company data)

### 5. Enterprise-oriented deployment path

* [ ] Hosted vector store
* [ ] API service layer
* [ ] Authentication and access control
* [ ] Document-level permissions
* [ ] IAM-based service access
* [ ] Logging and monitoring
* [x] Human review workflow for sensitive outputs (portfolio-minimal version: hold/flag queue, CLI approve/reject)

---

## Future direction

This project can be extended toward:

* compliance copilot workflows
* filing analysis assistants
* internal policy search
* investment research support tools
* auditable enterprise knowledge agents for regulated industries
