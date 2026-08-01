import os
from dotenv import load_dotenv

load_dotenv()

# AWS Bedrock
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
CHAT_MODEL_ID = os.getenv("CHAT_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
EMBEDDING_MODEL_ID = os.getenv("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")

# Vector store. The persist directory is env-overridable like the other runtime
# paths so the local reference runtime can point the API, the worker, and
# ingestion at one persistent directory. The default is unchanged.
CHROMA_PERSIST_DIR = os.getenv(
    "CHROMA_PERSIST_DIR",
    os.path.join(os.path.dirname(__file__), "chroma_db"),
)
CHROMA_COLLECTION = os.getenv("CHROMA_COLLECTION", "documents")

# Document corpus
DOCS_DIR = os.path.join(os.path.dirname(__file__), "docs")

# Chunking
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200

# Retrieval
RETRIEVAL_K = 5

# Web search
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

# Bedrock Guardrails (optional). When both are set, the agent attaches the
# guardrail to every model invocation. See policies/guardrails-policy.md.
BEDROCK_GUARDRAIL_ID = os.getenv("BEDROCK_GUARDRAIL_ID") or None
BEDROCK_GUARDRAIL_VERSION = os.getenv("BEDROCK_GUARDRAIL_VERSION") or None
BEDROCK_GUARDRAIL_TRACE = os.getenv("BEDROCK_GUARDRAIL_TRACE", "ENABLED")

# Audit logging
AUDIT_LOG_PATH = os.getenv(
    "AUDIT_LOG_PATH",
    os.path.join(os.path.dirname(__file__), "audit_logs", "query_audit.jsonl"),
)

# Human review queue (Phase 5). When HUMAN_REVIEW_HOLD is true and an answer is
# held for review, the user-facing answer is replaced with a short notice and the
# draft is captured in the queue item. When false (flag mode), the answer returns
# normally and the item is still enqueued. Queue files live under REVIEW_QUEUE_DIR.
HUMAN_REVIEW_HOLD = os.getenv("HUMAN_REVIEW_HOLD", "true").lower() == "true"
REVIEW_QUEUE_DIR = os.getenv(
    "REVIEW_QUEUE_DIR",
    os.path.join(os.path.dirname(__file__), "review_queue"),
)

# Filing identity (comparison roadmap step 2). The corpus manifest declares
# explicit filing identity metadata (company, form type, period_end,
# filing_date) for corpus files — values that must never be inferred from
# filenames or mtimes. The filing registry records one durable outcome per
# attempted source (parsed / duplicate / failed / conflict) plus the derived
# filing_id. Registry files are runtime state (gitignored), same as the
# review queue.
CORPUS_MANIFEST_PATH = os.getenv(
    "CORPUS_MANIFEST_PATH",
    os.path.join(os.path.dirname(__file__), "corpus_manifest.yaml"),
)
FILING_REGISTRY_PATH = os.getenv(
    "FILING_REGISTRY_PATH",
    os.path.join(os.path.dirname(__file__), "filing_registry", "registry.jsonl"),
)

# Persistent comparison entities (comparison roadmap step 3): SQLite, because
# comparisons are mutable workflow records that later grow state transitions
# and child rows — the documented JSONL limitations apply. Runtime state,
# gitignored like the other stores.
COMPARISON_DB_PATH = os.getenv(
    "COMPARISON_DB_PATH",
    os.path.join(os.path.dirname(__file__), "comparisons", "comparisons.db"),
)

# Controlled real-public-filing benchmark (Stage 3.5 step 9). The manifest is
# the frozen, committed pre-registration of what will be measured; the corpus
# directory holds downloaded filings, extracted sections, per-pair indexes,
# review packets, completed annotations, and run outputs — all gitignored
# runtime state, like chroma_db/ and the filing registry.
#
# SEC_USER_AGENT has NO default on purpose. Acquisition reaches an external
# public service that asks requesters to identify themselves; a baked-in
# default would be an anonymous request wearing someone else's name.
REAL_FILING_BENCHMARK_MANIFEST = os.getenv(
    "REAL_FILING_BENCHMARK_MANIFEST",
    os.path.join(os.path.dirname(__file__), "benchmarks", "real_filing_v1", "manifest.json"),
)
REAL_FILING_BENCHMARK_DIR = os.getenv(
    "REAL_FILING_BENCHMARK_DIR",
    os.path.join(os.path.dirname(__file__), "benchmark_data", "real_filings_v1"),
)
# The extraction holdout's local corpus lives beside the development corpus,
# under the same gitignored benchmark_data/ tree, and is never committed.
REAL_FILING_HOLDOUT_DIR = os.getenv(
    "REAL_FILING_HOLDOUT_DIR",
    os.path.join(os.path.dirname(__file__), "benchmark_data", "real_filing_holdout_v1"),
)
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT") or None

# Document ingestion. Two-tier PII redaction dispatch:
# - PII_REDACT_AT_INGEST (global, default off) forces redaction on every format.
# - PII_REDACT_TABULAR_AT_INGEST (default on) redacts CSV / XLSX only.
PII_REDACT_AT_INGEST = os.getenv("PII_REDACT_AT_INGEST", "false").lower() == "true"
PII_REDACT_TABULAR_AT_INGEST = (
    os.getenv("PII_REDACT_TABULAR_AT_INGEST", "true").lower() == "true"
)
