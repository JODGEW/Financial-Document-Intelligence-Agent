#!/usr/bin/env bash
# Start the API process of the local single-node reference runtime.
#
# NOT a production deployment. One machine, one process, local SQLite. There is
# no scheduler, daemon, job-processing loop, external queue, or multi-node
# coordination anywhere in this runtime.
#
# This script starts a server. It deliberately does NOT create the comparison
# database, the filing registry, or the vector store: initialization is a
# separate explicit operator action (see OPERATIONS.md), so a restart can never
# silently manufacture the state it was supposed to find.
#
#   cp .env.reference.example .env.reference   # then set FDIA_STATE_DIR
#   export FDIA_AUTH_SECRET=...                # supplied externally, never committed
#   scripts/run_reference_api.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="${FDIA_ENV_FILE:-.env.reference}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "./$ENV_FILE"
  set +a
fi

STATE_DIR="${FDIA_STATE_DIR:-./reference-state}"
export COMPARISON_DB_PATH="${COMPARISON_DB_PATH:-$STATE_DIR/comparisons/comparisons.db}"
export FILING_REGISTRY_PATH="${FILING_REGISTRY_PATH:-$STATE_DIR/filing_registry/registry.jsonl}"
export CHROMA_PERSIST_DIR="${CHROMA_PERSIST_DIR:-$STATE_DIR/chroma_db}"

HOST="${FDIA_API_HOST:-127.0.0.1}"
PORT="${FDIA_API_PORT:-8000}"

if [ -z "${FDIA_AUTH_SECRET:-}" ]; then
  echo "error: FDIA_AUTH_SECRET is required for the API process." >&2
  echo "Supply it externally; do not commit it. See OPERATIONS.md." >&2
  exit 2
fi

echo "reference runtime: api"
echo "  state directory: $STATE_DIR"
echo "  bind:            $HOST:$PORT"
echo "  worker:          manually invoked, one-shot (scripts/run_reference_worker.sh)"
echo
echo "Readiness is read-only and creates nothing:"
echo "  python scripts/check_runtime_readiness.py --role api \\"
echo "    --db-path \"\$COMPARISON_DB_PATH\" --registry-path \"\$FILING_REGISTRY_PATH\""
echo

exec python -m uvicorn api:app --host "$HOST" --port "$PORT"
