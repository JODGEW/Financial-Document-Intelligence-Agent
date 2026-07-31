# Controlled real-public-filing benchmark

A reproducible process for evaluating the existing Item 1A filing-change
workflow on **real** SEC 10-K filings.

```
frozen public filing manifest
  -> official-source acquisition        (explicit external network operation)
  -> SHA-256 verification
  -> Item 1A extraction                 (the existing ingestion/section path)
  -> existing comparison workflow       (the existing detector, unchanged)
  -> machine-proposed annotation packet
  -> EXPLICIT HUMAN VERIFICATION        (no tool performs this step)
  -> gold-label evaluation
  -> per-pair and aggregate report
```

## Current status: not complete, and no real-filing accuracy claim exists

| Stage | State |
|---|---|
| Infrastructure (schemas, acquisition, build, packets, evaluator, tests) | **implemented and offline-tested** |
| Corpus selection protocol | **frozen** (`benchmarks/real_filing_v1/manifest.json`) |
| Issuer slate | **frozen**, 10 issuers, 9 sector labels |
| CIK / accession / date / document / digest resolution | **not done** — requires network |
| Source verification | **not done** |
| Corpus build over real filings | **not done** |
| Human annotation | **not done** — zero labels are `human_verified` |
| Gold evaluation | **not done, and the evaluator refuses to produce one** |

The manifest's `status` is `proposed` and its `pairs` list is empty. Every
per-filing field is remote metadata; none of it was invented, recalled, or
approximated, because a wrong accession number in a frozen manifest is a
fabricated fact that later readers cannot distinguish from a real one.

**Stage 3 remains current.** Stage 3.5 remains in progress and stays in
progress until this corpus is source-verified, built, human-annotated,
evaluated, and reviewed.

---

## What is and is not committed

**Committed:** the frozen manifest, the schemas, the annotation protocol, the
evaluation config, the evaluation code, the tests, and bounded aggregate
reports containing no filing excerpts.

**Never committed** (`benchmark_data/` is gitignored): complete filings,
downloaded HTML, normalized parsed content, extracted Item 1A sections, local
Chroma indexes, temporary SQLite databases, annotation packets, completed
annotations, secrets, or tokens.

Local layout:

```
benchmark_data/real_filings_v1/
  sources/<pair_id>/<side>/<primary_document>   downloaded original
  sources/<pair_id>/<side>/acquisition.json     url, digest, timestamp, bytes
  build/<pair_id>/build.json                    bounded structural metadata
  build/<pair_id>/<side>_item_1a.txt            extracted section text
  build/<pair_id>/detection_result.json         comparison.v1 output
  build/<pair_id>/workspace/                    per-pair registry, index, db
  build/build_log.jsonl
  packets/<pair_id>/packet.{json,md}            human review material
  annotations/<pair_id>.machine_proposed.json   machine proposal
  annotations/<pair_id>.json                    the completed human file
  results/                                      local run outputs
```

`docs/` is the indexed corpus and **nothing from this benchmark goes there**.

---

## Corpus-selection protocol (`real-filing-selection.v1`)

Frozen **before** any filing was fetched and before any detector output was
observed. Full machine-readable form in
`real_filing_benchmark.SELECTION_CRITERIA`.

**Target:** 10 consecutive annual 10-K pairs, 20 filings, one issuer per pair,
at least 5 sector labels.

**Inclusion**

1. Fixed issuer identity by CIK.
2. Exact accession numbers, official filing dates, official reporting periods.
3. The primary 10-K document only — never a summary, exhibit, or third-party
   copy.
4. Both filings of a pair are the same issuer and the same document family.
5. The issuer slate is frozen before acquisition begins.

**Exclusion**

1. 10-K/A amendments are excluded from v1. An amendment case would be a
   separate, explicitly created case — never a substitution.
2. A filing that is missing or inaccessible is excluded **before** the manifest
   is frozen, never after.
3. **A pair is never replaced, reordered, or dropped after observing detector
   results.** A difficult pair stays in the corpus. Swapping it out is how a
   benchmark quietly becomes a demo.

**Stratification.** `sector_label` is recorded at selection time as
stratification metadata. It is never inferred, looked up, or recomputed during
evaluation, and it weights no metric.

**Representativeness.** The corpus is controlled and intentionally small. It is
**not** a statistically representative sample of SEC filings, issuers,
industries, or filing formats, and no metric computed over it may be presented
as one.

---

## Manifest maturity ladder

`proposed` → `source_verified` → `corpus_built` → `human_annotation_complete`

Each status asserts artifacts the previous one produced. A status advances one
documented step at a time, forward only, in a reviewed commit —
`validate_status_transition` rejects skips and regressions. A placeholder digest
is accepted only while `proposed`; an empty `pairs` list likewise.

---

## Commands

### 1. Resolve the slate (network)

```bash
export SEC_USER_AGENT="Jane Doe Research jane.doe@university.edu"
python scripts/fetch_real_filing_benchmark.py --allow-network --resolve
```

Reads official submission metadata for each slate entry and writes a **local
proposal** (`proposed_pairs.json`). It never edits the committed manifest: an
accession number that lands in a frozen benchmark arrives via a reviewed
commit, not a tool's side effect.

CIK lookup by company name is deliberately not automated — a name match is
ambiguous across subsidiaries and former registrants, and picking one silently
would fabricate an identity. Look each CIK up on EDGAR and record it in the
slate first.

### 2. Acquire (network)

```bash
python scripts/fetch_real_filing_benchmark.py --allow-network
python scripts/fetch_real_filing_benchmark.py --allow-network --pair-id energy-01
python scripts/fetch_real_filing_benchmark.py --allow-network --record-hashes
```

Behavior:

- **Network is off by default.** `--allow-network` is required; nothing turns it
  on implicitly.
- **`SEC_USER_AGENT` is required** and must be descriptive — SEC asks
  requesters to identify themselves. Missing, malformed, and placeholder values
  (`your-email@example.com`, `<your name>`, `TODO …`) are rejected rather than
  sent.
- Only official SEC hosts, matched by **exact hostname equality** over https —
  `www.sec.gov.attacker.test` is refused, as are mirrors and caches.
- Conservative configurable request interval (default 1.0s).
- Bounded retry (default 3 attempts) for transport faults and an explicit
  transient-status allowlist (429/500/502/503/504) only. 403, 404, and
  everything else fail immediately. This policy is **independent of the
  detection-job retry policy** — external-fetch backoff and workflow-lifecycle
  retry are different problems.
- `Retry-After` honored when present, capped at 120s.
- Per-request timeouts, atomic writes, verified-cache reuse, SHA-256 verified
  **before** bytes reach the corpus directory.
- A cached file whose digest disagrees with the manifest is **preserved, not
  overwritten** — silent content replacement destroys the evidence of the
  disagreement.
- Output prints counts, digests, and corpus-relative paths. Never filing
  content, never an absolute local path.

`--record-hashes` downloads bytes for a still-`proposed` manifest and records
observed digests locally for a human to freeze. It **always exits nonzero**,
because recording a digest you just computed verifies nothing.

Exit codes: `0` every requested filing verified · `1` network, checksum, or
source failure · `2` invalid configuration or arguments.

### 3. Build the corpus (offline)

```bash
python scripts/build_real_filing_benchmark.py
python scripts/build_real_filing_benchmark.py --pair-id energy-01 --json
```

Verifies checksums, parses through the existing ingestion path, mints filing
identity through the existing registry contracts, extracts Item 1A through the
existing section-identification path, and runs the existing comparison
workflow. Offline and credential-free: Chroma is seeded with a deterministic
local embedding function that **raises if a query embedding is ever requested**,
so the detector's metadata-only reads are proven rather than asserted.

Per side it records an extraction outcome — `extracted`, `missing`,
`ambiguous`, or `parse_failed` — plus source hash, section hash, detected
heading, character count, paragraph count, unit count, deterministic unit ids,
and parser versions. Section text is written only into the gitignored corpus
directory.

**The extraction algorithm is not modified to improve this benchmark.** The
existing section path derives `section_key` from splitter-produced headings
only. A filing whose Item 1A heading is not emitted as a heading by its loader
records as `missing`, and that is the honest, reportable result — not a reason
to loosen a heading rule.

Identical inputs produce an identical `build_hash` (wall-clock stamps and the
attempt ids minted from them are excluded).

### 4. Generate annotation packets (offline)

```bash
python scripts/create_real_filing_annotation_packets.py
python scripts/create_real_filing_annotation_packets.py --validate <file>
```

Writes local `packet.json` + `packet.md` and a machine-proposed annotation
file per pair. Every proposal is marked, excerpts are capped at 400 characters,
and complete sections never enter a packet. `--validate` is the import
validator for a completed annotation file.

### 5. Human verification

See [`benchmarks/real_filing_v1/annotation_protocol.md`](benchmarks/real_filing_v1/annotation_protocol.md).

`machine_proposed` vs `human_verified`, stated once more because it is the
whole point: a machine proposal is the detector's opinion about its own output.
Scoring the detector against it measures nothing. The schema makes the
distinction structural — a machine status may carry no annotator identity, and
`human_verified` requires an explicit `annotator_id` and
`verification_timestamp` that **no tool in this repository sets**.

There is no authentication for benchmark annotators; `annotator_id` is
self-asserted local metadata and is labelled as such.

### 6. Evaluate (offline)

```bash
python scripts/eval_real_filing_benchmark.py --unlabeled     # execution report
python scripts/eval_real_filing_benchmark.py                 # gold evaluation
python scripts/eval_real_filing_benchmark.py --json --report out.json
```

**Unlabeled mode** reports corpus-build outcomes, execution outcomes, output
and undetermined counts, runtime, failure codes, and evidence-resolution
mechanics. It contains no accuracy metric, states so in its payload, and cannot
support a quality claim.

**Gold mode refuses** unless every requested pair is `human_verified`, section
hashes still match the build, source checksums still match the manifest, every
unit reference exists, and the live detector/workflow versions match
`evaluation_config.json` (override with `--new-run` to create an explicitly new
run). Every applicable reason is reported, not just the first.

Exit codes: `0` report produced · `1` refused or a pair failed · `2` invalid
configuration or arguments.

---

## Metric definitions

Every rate reports numerator and denominator. A zero denominator reports
`value: null` — never `0`, never `NaN`, never omitted — because a rate over
nothing asserts nothing.

**Corpus quality** (counts): `pairs_requested`, `pairs_source_verified`,
`pairs_built`, `pairs_extracted`, `pairs_missing_section`,
`pairs_ambiguous_section`, `pairs_parse_failed`, `pairs_human_verified`.

**Gold metrics**, over human-verified pairs only:

| Metric | Definition |
|---|---|
| `change_precision` | Same as the synthetic suite. Matched (unit_key, change_type) over every emitted change; an unrecoverable change counts in the denominator. |
| `change_recall` | Same as the synthetic suite. Denominator: labels whose type is not `unchanged`. |
| `change_type_accuracy` | Same as the synthetic suite. Denominator: labels whose unit was detected at all — a missed unit is a recall failure, not double-counted here. |
| `unchanged_false_positive_rate` | Same as the synthetic suite. `unchanged` asserts absence; the numerator counts unchanged-labelled units that were emitted. |
| `evidence_resolution_rate` | Same as the synthetic suite. References resolving to an indexed chunk of the correct filing. |
| `undetermined_reason_accuracy` | Same as the synthetic suite, **but the real-filing denominator differs**: labels carrying a reason code *and* whose unit the detector reported as undetermined. |
| `direction_consistency_accuracy` | **New, and its definition differs.** `comparison.v1` carries no direction field, so this scores the detector's `direction_consistency` validator against a human's directional claim. Denominator: matched changes with a labelled direction — typically far smaller than the change denominators. |
| `pair_exact_match_rate` | **New.** A pair matches iff the detected set equals the labelled non-unchanged set and no unchanged unit was emitted. |

**Operational metrics**: detection duration p50/p95 (nearest-rank, the same
single definition `comparison_reliability` uses), total attempts, retries,
detection jobs, lease reclaims, failures by stable code, and human-review label
counts with a confidence distribution. Reviewer notes are excluded from every
report.

The benchmark build calls the detector **directly and synchronously**, so it
creates no durable job, lease, or retry. Zero jobs and zero reclaims mean that
path was not exercised — not that it succeeded.

### No thresholds

There is **no pass/fail gate** on any real-filing metric, and none may be
introduced until an initial human-verified baseline exists and has been
explicitly approved. `evaluation_config.json` carries
`"pass_fail_thresholds": null` deliberately.

---

## CI

The merge-blocking required check remains **`comparison-regression`**, and the
**synthetic comparison regression suite remains the deterministic gate**. This
commit adds one offline step to it — manifest and annotation schemas,
mocked-HTTP acquisition, corpus build over tiny synthetic HTML fixtures, and
evaluator refusal/metric behavior.

Required CI **never contacts SEC EDGAR**, never supplies a user agent, and
never downloads a filing. Acquisition is an explicit, manually invoked external
operation and is not a substitute for merge-blocking offline validation.

Nothing about the workflow's identity changed: same name, job id, job name,
triggers, no path filters, no secrets, no AWS credentials, same regression
evaluator, same artifact upload, same existing suites. The synthetic label file
and its frozen baseline are untouched.

---

## Reproducing a run

```bash
git rev-parse HEAD                                              # 1. commit
sha256sum benchmarks/real_filing_v1/manifest.json               # 2. manifest hash
export SEC_USER_AGENT="Your Name your.email@your.org"
python scripts/fetch_real_filing_benchmark.py --allow-network   # 3. sources
python scripts/build_real_filing_benchmark.py                   # 4. corpus
python scripts/create_real_filing_annotation_packets.py         # 5. packets
#                                                               # 6. human review
python scripts/eval_real_filing_benchmark.py --json --report run.json
```

Every report carries detector version, workflow version, commit SHA, manifest
hash, annotation hash, metric-definitions version, and evaluation timestamp, so
a number can always be traced to the exact inputs that produced it.

---

## Known coverage limitations

- **No accuracy claim exists.** Zero labels are `human_verified`.
- Item 1A only. No other SEC section is extracted or compared.
- 10-K only; amendments excluded from v1.
- 10 pairs across 10 large-cap issuers. Small, controlled, and not
  representative of smaller registrants, foreign private issuers, or unusual
  filing formats.
- **No inter-annotator agreement.** One annotator per pair, so no agreement
  statistic exists and none is reported.
- Extraction depends on the loader emitting Item 1A as a heading. Real EDGAR
  HTML frequently does not, so `missing` and `ambiguous` outcomes are expected
  and are reported rather than worked around.
- The detector remains exact heading/content alignment with no similarity
  stage; a reworded-and-retitled risk factor reports as added + removed.
- Direction consistency scores a validator verdict, not a detector-emitted
  direction.
- Acquisition is single-process, single-node, and best-effort against a public
  service. It is not a data-ingestion service.
