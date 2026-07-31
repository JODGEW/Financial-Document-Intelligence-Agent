# Operations runbooks — local single-node reference runtime

Operator procedures for the durable comparison-detection workflow.

**Read this first.** This is a **single-node reference runtime**, not a
production deployment. There is no scheduler, no daemon, no job-processing
loop, no background poller, no external queue, no distributed lease, and no
multi-node coordination. **Nothing happens because time passed.** A queued job,
an expired lease, and a due retry all stay exactly as they are until an
operator runs the one-shot worker again. Every runbook below assumes that.

Two more limits apply everywhere and are not repeated in each entry:

- Local SQLite records and process logs are **application records, not
  tamper-proof storage**. Anyone with file access can alter them.
- There is **no exactly-once guarantee** and no high-availability claim.

**Never edit SQLite rows by hand.** Every state transition in this workflow is
a fenced transaction that also writes events and maintains cross-table
coherence. A manual `UPDATE` bypasses the fence, desynchronizes the job,
attempt, comparison, and result, and turns a recoverable situation into an
unreadable one. Every permitted action below is an existing command.

---

## Common diagnostics

All read-only. None of them creates, migrates, repairs, retries, reclaims, or
mutates anything.

```bash
# Current state, unresolved issues, and recent failures.
python scripts/comparison_reliability_report.py --db-path "$COMPARISON_DB_PATH"
python scripts/comparison_reliability_report.py --db-path "$COMPARISON_DB_PATH" --issues --failures
python scripts/comparison_reliability_report.py --db-path "$COMPARISON_DB_PATH" --json

# Can this process do its job right now?
python scripts/check_runtime_readiness.py --role api \
  --db-path "$COMPARISON_DB_PATH" --registry-path "$FILING_REGISTRY_PATH"
python scripts/check_runtime_readiness.py --role worker \
  --db-path "$COMPARISON_DB_PATH" --registry-path "$FILING_REGISTRY_PATH" \
  --persist-dir "$CHROMA_PERSIST_DIR"
```

**Initialization is separate from readiness.** Readiness is strictly read-only:
it never creates a database, a table, a migration, a registry, or Chroma
content. A `comparison_database_unavailable` result means the store has not
been initialized yet (or is not reachable) — it does not mean readiness should
have created it. Initialize deliberately, once, by running `python ingest.py`
(registry + vector store) and letting the first workflow write create the
comparison database, then re-run readiness.

---

## 1. API restart after a lost response

**Observable state.** A client's `POST /api/comparisons/{id}/detect` returned
no response — connection reset, timeout, or the process died. The caller does
not know whether the job was queued.

**Diagnose.**
```bash
python scripts/comparison_reliability_report.py --db-path "$COMPARISON_DB_PATH" --issues
```
Look for `queued_detection_job` on that comparison.

**Permitted action.** Restart the API and **repeat the identical request**. The
enqueue is a single committed transaction and the route is idempotent: the
repeat returns `202` with `created: false` and the *same* `jobId`. If the
commit had not happened, the repeat queues it once. Either way exactly one
durable job exists.

**Do not.** Do not create a second comparison to "retry". Do not insert a job
row. Do not assume the absent response means the work was lost — the response
is not the record; the commit is.

**Known single-node limitation.** The API never executes a detector, so a lost
response can never mean partial detection. The job still needs a manual worker
invocation (runbook 2).

---

## 2. A queued job is not being processed

**Observable state.** Comparison status `queued_for_detection`; reliability
issue `queued_detection_job` with action code `run_one_shot_detection_worker`.
No attempt exists.

**Diagnose.**
```bash
python scripts/comparison_reliability_report.py --db-path "$COMPARISON_DB_PATH" --issues
```

**Permitted action.** Run the worker. This is expected, not a fault: **there is
no scheduler, so a queued job waits indefinitely by design.**
```bash
scripts/run_reference_worker.sh                       # any eligible job
scripts/run_reference_worker.sh --job-id djob_xxxxx   # one specific job
```

**Do not.** Do not add a cron entry, a systemd timer, a supervisor restart
loop, or a wrapper that polls. Automatic invocation is outside this
architecture, and a restart policy that re-runs the worker on exit would
silently turn a one-shot command into an unbounded loop.

**Known single-node limitation.** One worker, one job per invocation.

---

## 3. An expired worker lease

**Observable state.** Job `running`, attempt `running`, comparison `detecting`,
but the lease is past its expiry. Reliability issue
`expired_detection_job_lease`, action code `run_one_shot_worker_to_reclaim`.
Typically the worker process died.

**Diagnose.**
```bash
python scripts/comparison_reliability_report.py --db-path "$COMPARISON_DB_PATH" --issues --json
```

**Permitted action.** Run the worker again. Strictly after expiry **plus the
policy's reclaim grace**, one invocation atomically retires the old attempt as
`timed_out`, creates a replacement attempt, increments the claim generation,
and issues a fresh lease and token.
```bash
scripts/run_reference_worker.sh
```

**Do not.** Do not reclaim before expiry plus grace — the store refuses it, and
that refusal is what stops two workers from executing the same job. Do not
extend the lease by hand. Do not mark the attempt `failed` to "clear" it.

**Known single-node limitation.** Expiry alone changes nothing. Recovery
latency is exactly how long it takes an operator to run the command.

---

## 4. A fenced old worker

**Observable state.** A worker that was paused or partitioned resumes, finds it
no longer owns the job, and exits reporting lost ownership. Its generation is
behind the job's current generation. Logs show
`detection_job_finalize_rejected`.

**This is the fence working.** The old generation cannot heartbeat, cannot
commit success, and cannot commit failure against the replacement. Exactly one
result exists, produced by the current owner.

**Diagnose.**
```bash
python scripts/comparison_reliability_report.py --db-path "$COMPARISON_DB_PATH" --json
```
Confirm the job's `claimGeneration` and current `workerId`.

**Permitted action.** None on the workflow. Let the old process exit. If the
job is still `running` under the *current* generation, follow runbook 3.

**Do not.** Do not "help" the old worker by lowering the generation, reissuing
its token, or re-running it against the same attempt id.

**Known single-node limitation.** Fencing is enforced inside the terminal
SQLite transaction. It protects state integrity; it does not stop the old
process from having burned wall-clock time.

---

## 5. A retry is waiting, and claiming a due retry

**Observable state.** Job `retry_wait`, comparison
`waiting_for_detection_retry`, `retryCount >= 1`, and a `nextAttemptAt` in the
future. Reliability issue `detection_job_waiting_for_retry`. No replacement
attempt exists yet — that is correct.

**Diagnose.**
```bash
python scripts/comparison_reliability_report.py --db-path "$COMPARISON_DB_PATH" --issues --json
```

**Permitted action.** Wait until `nextAttemptAt`, then run the worker. A due
retry is claimable at or after that instant, and the claim creates a fresh
attempt, generation, token, lease, and heartbeat.
```bash
scripts/run_reference_worker.sh
```
Before the due time the worker correctly reports no eligible work and changes
nothing. If a retry has been due for a while, the issue becomes
`detection_job_retry_overdue` — same action.

**Do not.** Do not shorten the delay to force an early run. Do not create a new
comparison to bypass the wait. Do not add a timer that fires at `nextAttemptAt`.

**Known single-node limitation.** Reaching `nextAttemptAt` executes nothing.
The due time makes work *eligible*; only an explicit invocation claims it.

---

## 6. Retry exhaustion

**Observable state.** Job `failed` with `detection_job_retries_exhausted` (or
`detection_job_execution_budget_exhausted`). Reliability issue
`detection_job_retries_exhausted`, action code `inspect_failure`. Terminal.

**Diagnose.**
```bash
python scripts/comparison_reliability_report.py --db-path "$COMPARISON_DB_PATH" --failures --json
```
Read `lastFailureCode` and `lastFailureClassification`. Only explicitly
allowlisted transient codes ever retried; a deterministic, integrity,
configuration, or unknown-internal failure fails closed on its first
occurrence, by design.

**Permitted action.** Fix the underlying cause — a missing section, an
incomplete manifest entry, an unavailable dependency — then create a **new
comparison** and detect it. Exhaustion is a real terminal outcome, and the
record of it is kept deliberately.

**Do not.** Do not reset `retry_count`. Do not move the job back to `queued`.
Do not raise `max_retry_attempts` to force another pass at an unchanged,
deterministically failing input.

**Known single-node limitation.** No dead-letter queue and no automatic
escalation; exhaustion is visible only where you look for it.

---

## 7. Claim-generation exhaustion

**Observable state.** Job `failed` with `detection_job_claims_exhausted`.
Reliability issue `detection_job_claims_exhausted`. The job consumed every
ownership generation the lease policy allows (initial claim plus reclaims and
retry claims combined) without reaching a terminal result.

**Diagnose.**
```bash
python scripts/comparison_reliability_report.py --db-path "$COMPARISON_DB_PATH" --issues --failures --json
```
Repeated generation loss almost always means workers are dying mid-execution.
Check whether the host is killing them (OOM, container limits, a wrapper that
terminates long commands).

**Permitted action.** Fix the reason workers are not surviving, then create a
new comparison. The generation cap is the hard combined bound on execution
ownership and it is intentionally not resettable.

**Do not.** Do not raise `max_claim_generations` to get past a job that keeps
losing its worker — that converts a visible bound into an invisible loop. Do
not hand-edit `claim_generation`.

**Known single-node limitation.** The cap counts reclaims and retry claims
together; it is a bound on execution ownership, not on retry policy alone.

---

## 8. SQLite lock contention

**Observable state.** An API request returns `500` with code
`comparison_storage_error` and an `error_id`, or the worker CLI exits `1` with
`worker_infrastructure_error: execution could not be completed`. Both are
stable, safe messages: no SQL, path, or SQLite error text reaches the client.

**Cause.** Another process held a write transaction for longer than the store's
`busy_timeout` (5000 ms). Usually a second worker, a backup taken the wrong
way, or a manually opened `sqlite3` shell left in a transaction.

**Diagnose.** Find the other writer. Correlate the `error_id` with the server
log for the full server-side detail.

**Permitted action.** Release the other writer, then **re-run the explicit
command**. A timed-out acquisition applies nothing at all — the operation is
whole or absent, never half — so a retry is safe and reaches a valid state.

**Do not.** Do not raise `busy_timeout` to paper over a stuck writer. Do not
copy the database with `cp` while writers are active; use runbook 10. Do not
retry in a loop — no implicit automatic retry exists here and adding one would
hide the contention.

**Known single-node limitation.** SQLite allows one writer at a time. This
runtime is designed around that, not despite it.

---

## 9. Auth-secret rotation

**Observable state.** You need to replace `FDIA_AUTH_SECRET`.

**Understand the blast radius first.** Tokens are signed with this secret and
there is no revocation list: **rotation invalidates every outstanding token
immediately.** Anyone holding the secret can mint tokens for any role.

**Permitted action.**
```bash
# Generate without leaving it in shell history.
python -c "import secrets; print(secrets.token_urlsafe(48))"
```
Set the new value in the API process's environment, restart the API, and reissue
tokens:
```bash
python scripts/issue_local_access_token.py --subject operator@example.local --role operator
```
The worker is credential-free and is **not** affected — do not restart it for a
rotation.

**Do not.** Do not commit the secret, put it in `.env.reference.example`, pass
it as a command-line argument, or redirect an issued token into a tracked file.
Do not rotate while a client holds a token it cannot re-request.

**Known single-node limitation.** One shared local secret, no revocation, no
key rotation overlap window, no OAuth/OIDC/SSO.

---

## 10. Workflow-database backup and restore

**Scope — read this before relying on it.** The utility backs up the
**comparison workflow database only**. It does **not** cover the filing
registry (`filing_registry/registry.jsonl`) or the vector store
(`chroma_db/`). A restored database can reference filings that the registry and
index no longer describe. **A SQLite-only backup is not a complete
filing-workflow backup.** A real recovery is a coordinated restore of all three
artifacts captured at a consistent point.

**Permitted action.**
```bash
# Consistent copy via SQLite's online backup API; the source is never modified.
python scripts/backup_workflow_db.py --db-path "$COMPARISON_DB_PATH" --out workflow-backup.db

# Overwriting is refused unless you ask for it explicitly.
python scripts/backup_workflow_db.py --db-path "$COMPARISON_DB_PATH" --out workflow-backup.db --force

# Verify a backup is an independently readable, coherent database.
python scripts/backup_workflow_db.py --verify workflow-backup.db
```
Every backup is integrity-checked before success is reported. To restore, stop
the API, ensure no worker is running, move the backup into place as the
comparison database, verify it, and restore the matching registry and vector
store from the same point in time.

**Do not.** Do not `cp` a live database. Do not restore the database alone and
call the system recovered. Do not include the auth secret or a token in a
backup artifact.

**Known single-node limitation.** No point-in-time recovery, no WAL archiving,
no automated schedule, and no cross-artifact consistency guarantee — the three
artifacts are quiesced by you, not by the system.

---

## 11. Filing registry or vector store unavailable

**Observable state.** Readiness reports `filing_registry_unavailable` or
`vector_store_unavailable`. Reliability may refuse with
`reliability_dependency_unavailable`. A worker exits `1` with
`worker_infrastructure_error: required local storage is unavailable`.

**Diagnose.**
```bash
python scripts/check_runtime_readiness.py --role worker \
  --db-path "$COMPARISON_DB_PATH" --registry-path "$FILING_REGISTRY_PATH" \
  --persist-dir "$CHROMA_PERSIST_DIR"
```
Both are gitignored runtime state, so a clean clone legitimately lacks them.

**Permitted action.** Rebuild them deliberately:
```bash
python ingest.py
```
This repopulates the registry and the vector store from `docs/` and the corpus
manifest. Then re-run readiness. Queued and claimed jobs are unaffected and
remain claimable afterwards.

**Do not.** Do not hand-write registry lines. Do not point the runtime at a
vector store built from a different corpus — detection would resolve evidence
against chunks the registry does not describe. Do not treat the refusal as
"nothing to do": an unanswerable dependency is reported as a refusal precisely
so it is not mistaken for a clean result.

**Known single-node limitation.** Registry writes are single-process and
unprotected across processes; run `ingest.py` alone.

---

## 12. Collecting a reliability report

**When.** Before escalating anything, and when capturing the state of an
incident.

**Permitted action.**
```bash
python scripts/comparison_reliability_report.py --db-path "$COMPARISON_DB_PATH" \
  --issues --failures --json > reliability-report.json
```
The report is read-only and safe to share: stable codes, identifiers, counts,
and rates only — never evidence, filing text, reviewer or operator notes, SQL,
schema, absolute paths, or exception text. Exit `0` means a valid report **even
when unresolved issues exist**; `1` means the report was refused (storage
unreadable, records invalid, or an unanswerable registry) with nothing on
stdout; `2` means bad arguments or a `--db-path` that does not exist.

**Do not.** Do not treat exit `0` as "healthy" — read the issue list. Do not
treat a refusal as an empty system; a store that cannot be observed is not a
store with nothing in it.

**Known single-node limitation.** No dashboard, alerting, notification, or
external monitoring integration. Reports are pull-only, and an issue keeps
appearing for exactly as long as the record producing it exists — there is no
acknowledgement state.
