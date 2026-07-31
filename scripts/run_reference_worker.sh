#!/usr/bin/env bash
# Run ONE detection job in the local single-node reference runtime.
#
# This is a one-shot process, on purpose. It claims at most one eligible job,
# executes it, and exits. Running it does not start a loop, a daemon, a
# scheduler, or a background poller, and nothing in this runtime invokes it for
# you — a queued job, an expired lease, and a due retry all stay exactly where
# they are until an operator runs this command again.
#
# Credential-free: no FDIA_AUTH_SECRET, no AWS credentials, no network. It uses
# the same persistent state as scripts/run_reference_api.sh.
#
#   scripts/run_reference_worker.sh                      # any eligible job
#   scripts/run_reference_worker.sh --job-id djob_...    # one specific job
#
# Any extra arguments are passed through to the worker CLI.

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

# The worker never needs the API's signing secret. Clear it so a shell that
# happens to export one cannot make this process look credentialed.
unset FDIA_AUTH_SECRET

WORKER_ID="${FDIA_WORKER_ID:-reference-worker-$$}"

exec python scripts/run_comparison_detection_worker.py \
  --db-path "$COMPARISON_DB_PATH" \
  --registry-path "$FILING_REGISTRY_PATH" \
  --worker-id "$WORKER_ID" \
  --once \
  "$@"
