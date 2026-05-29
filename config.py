import os
from dotenv import load_dotenv

load_dotenv()

# AWS Bedrock
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
CHAT_MODEL_ID = os.getenv("CHAT_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
EMBEDDING_MODEL_ID = os.getenv("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")

# Vector store
CHROMA_PERSIST_DIR = os.path.join(os.path.dirname(__file__), "chroma_db")
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

# Document ingestion. Two-tier PII redaction dispatch:
# - PII_REDACT_AT_INGEST (global, default off) forces redaction on every format.
# - PII_REDACT_TABULAR_AT_INGEST (default on) redacts CSV / XLSX only.
PII_REDACT_AT_INGEST = os.getenv("PII_REDACT_AT_INGEST", "false").lower() == "true"
PII_REDACT_TABULAR_AT_INGEST = (
    os.getenv("PII_REDACT_TABULAR_AT_INGEST", "true").lower() == "true"
)
INGEST_TABLE_EXTRACTION = (
    os.getenv("INGEST_TABLE_EXTRACTION", "true").lower() == "true"
)
