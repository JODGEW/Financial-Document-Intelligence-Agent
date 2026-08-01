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

## ⚠ Corpus role: this is a DEVELOPMENT corpus, not a holdout

`real_filings_v1` is machine-marked `corpus_role = extraction_development_corpus`
in every v2 report. Read this before quoting any extraction number from it.

The twenty source documents were **inspected to diagnose HTML structure while
the SEC Item heading parser (`sec_html_item_headings.v2`) was being designed**.
The parser was built to handle the element shapes those filings actually use.
That is legitimate engineering, and it is also exactly what disqualifies these
filings as an independent test of the parser.

Therefore:

- The frozen issuer slate and source documents were **not replaced**. Same ten
  issuers, same twenty filings, same manifest hash, same twenty source digests.
  Nothing was swapped after a result was seen.
- The corpus **remains valid** for reproducibility, for diagnostic integration
  testing, and as the artifact that exposed the ingestion gap in the first
  place. Its v1 null-extraction reports stay committed as the before-picture.
- Its HTML structures **were inspected during extraction v2 development**.
- **20/20 extraction is an in-sample development result.** It measures the
  parser's fit to documents it was built against.
- It is **not evidence of extraction generalization** to unseen filings.
- Downstream detector labels remain untouched, and **zero labels are
  `human_verified`**.
- Any accuracy evaluation over these ten pairs must be described as
  **development or diagnostic evaluation** — never as a benchmark result, a
  holdout measurement, or an accuracy claim.
- **Stage 3.5 completion requires a separately frozen holdout corpus**, whose
  issuers and filings are selected only *after* extraction v2 is frozen, and
  which is then source-verified, extracted, annotated, and evaluated **without
  changing extraction v2**.

Machine-readable in `corpus_build_report.v2.json`, `execution_report.v2.json`,
and `annotation_packet_inventory.v2.json`:

```json
{
  "corpus_role": "extraction_development_corpus",
  "extraction_parser_developed_using_this_corpus": true,
  "extraction_holdout_evaluation": false,
  "generalization_claim_supported": false
}
```

The closed vocabulary lives in `real_filing_benchmark.CORPUS_ROLES`; a role
outside it raises `CorpusRoleError`. `generalization_claim_supported` is false
unconditionally until a holdout corpus is frozen *and* annotated.

---

## Current status: not complete, and no real-filing accuracy claim exists

| Stage | State |
|---|---|
| Infrastructure (schemas, acquisition, build, packets, evaluator, tests) | **implemented and offline-tested** |
| Corpus selection protocol | **frozen** (`benchmarks/real_filing_v1/manifest.json`) |
| Issuer slate | **frozen**, 10 issuers, 9 sector labels |
| CIK / accession / date / document / digest resolution | **done** — resolved from official SEC endpoints |
| Source verification | **done** — 20/20 filings SHA-256 verified |
| Corpus role | **development, not holdout** — inspected while extraction v2 was written |
| Corpus build over real filings | **done, in-sample** — 20/20 sides `extracted` (was 0/20; see below) |
| Item 1A comparison workflow over real filings | **executed** — 10/10 pairs reached `detected` |
| Human annotation | **not done** — zero labels are `human_verified`; 10 machine-proposed packets exist |
| Gold evaluation | **not done, and the evaluator refuses to produce one** |
| Extraction holdout corpus | **frozen, metadata-only** (`benchmarks/real_filing_holdout_v1/`, status `holdout_frozen_metadata_only`) — bodies not acquired, nothing extracted, nothing annotated |

The manifest's `status` is `source_verified`. Every per-filing field was
resolved from an official SEC endpoint; none of it was invented, recalled, or
approximated, because a wrong accession number in a frozen manifest is a
fabricated fact that later readers cannot distinguish from a real one.

**Stage 3 remains current.** Stage 3.5 remains in progress.

### Next steps, in order

1. **Human diagnostic annotation of the ten development pairs.** Verify that
   the extracted sections and detected changes are correct on these filings.
   This is diagnostic evaluation of the development corpus; completing it does
   not produce a generalization claim and does not complete Stage 3.5.
2. ~~Fix the production Chroma batching defect this corpus exposed.~~ **Done** —
   production and benchmark ingestion now share one bounded-write helper
   ([chroma_batching.py](chroma_batching.py), documented below).
3. ~~Freeze a new unseen holdout corpus.~~ **Done, metadata-only** — ten
   issuer pairs frozen from official SEC metadata after extraction v2 was
   merged, with no one having inspected their HTML
   (`benchmarks/real_filing_holdout_v1/`, documented below).
4. **Run source verification, extraction, annotation, and evaluation on that
   holdout without changing extraction v2.** If extraction is modified in
   response to holdout results, the holdout becomes a development corpus too
   and a fresh one is required.

### The prior null result, and what changed

The first build over these filings extracted **nothing**: 20/20 sides recorded
`missing`. Root cause: real EDGAR 10-K HTML carries **no** `<h1>`–`<h6>`
elements at all — Item headings are styled `<div>` / `<span>` / `<p>` /
table-cell blocks — and the HTML loader derived `section_title` from heading
tags only, so no chunk ever carried the canonical Item 1A key.

That result is **preserved unchanged** as the null baseline
(`corpus_build_report.json`, `execution_report.json`,
`annotation_packet_inventory.json`). It was not overwritten, and it is the
before-picture the current numbers are measured against.

The ingestion gap was then closed generically (`loaders/sec_headings.py`,
parser version `sec_html_item_headings.v2`) and the **same** frozen corpus was
rebuilt. All 20 sides now extract and all 10 pairs reach `detected`.

Stated precisely, because the distinction is the whole point of this benchmark:

- **The 20/20 result is in-sample.** These filings were inspected to diagnose
  structure while the parser was designed, so the corpus is an extraction
  *development* corpus. See the corpus-role section at the top.
- Extraction changed **after** the frozen source corpus revealed an ingestion
  gap. The gap was diagnosed from HTML *structure* — which element types carry
  Item headings — never from detector output.
- The source corpus and issuer selection are **unchanged**: same manifest, same
  manifest hash, same twenty filings, same twenty source digests. No pair was
  replaced, and no issuer was swapped after seeing a result.
- **No detector, alignment, validator, governance threshold, or benchmark label
  was used to tune extraction**, and none of them was modified.
- The old null-extraction results **remain recorded**.
- Machine annotation is **not gold**. Zero labels are `human_verified`.
- **No real-filing accuracy claim exists** and none can until human review.

### Committed reports

The v1 reports are the null-extraction baseline; the v2 reports describe the
rebuild. Both are committed, and a new build never overwrites a prior report.

| File | Contents |
|---|---|
| `source_verification_report.json` | per-filing official URL, digest, byte count, acquisition provenance |
| `corpus_build_report.json` | **v1 baseline** — the 0-of-20 null extraction |
| `corpus_build_report.v2.json` | per-pair build hash, per-side outcome, extraction diagnostics |
| `execution_report.json` | **v1 baseline** — unlabeled report for a workflow that did not run |
| `execution_report.v2.json` | unlabeled execution report for the rebuild (no accuracy metric) |
| `annotation_packet_inventory.json` | **v1 baseline** — zero packets, all pairs blocked |
| `annotation_packet_inventory.v2.json` | packet inventory, review readiness, blocking reasons |
| `HUMAN_REVIEW_CHECKLIST.md` | what a reviewer does per pair |

Every JSON report carries identifiers, hashes, counts, version strings, and
outcome codes only — no filing content, no section text, no excerpt, no local
path, no credential.

### Per-side outcomes, before and after

| Pair | v1 prev → curr | v2 prev → curr | Changes | Units prev/curr |
|---|---|---|---|---|
| communication-services-01 | missing → missing | extracted → extracted | 4 | 5 / 5 |
| consumer-discretionary-01 | missing → missing | extracted → extracted | 1 | 1 / 1 |
| consumer-staples-01 | missing → missing | extracted → extracted | 1 | 1 / 1 |
| energy-01 | missing → missing | extracted → extracted | 1 | 1 / 1 |
| financials-01 | missing → missing | extracted → extracted | 1 | 1 / 1 |
| health-care-01 | missing → missing | extracted → extracted | 2 | 2 / 1 |
| industrials-01 | missing → missing | extracted → extracted | 1 | 1 / 1 |
| information-technology-01 | missing → missing | extracted → extracted | 4 | 6 / 6 |
| information-technology-02 | missing → missing | extracted → extracted | 1 | 1 / 1 |
| utilities-01 | missing → missing | extracted → extracted | 5 | 5 / 5 |

Zero sides remain `missing`, `ambiguous`, or `parse_failed`. All ten pairs are
buildable and annotatable; ten machine-proposed packets exist and **zero** are
verified.

Change counts and unit counts are **execution mechanics, not accuracy**. In
particular the low unit counts are a *detector* limitation, not an extraction
one: `comparison_detector._HEADING_RE` only treats a line as a risk-factor
heading when it ends in `Risk`/`Risks`, and most real filings write
sentence-style risk headings, so those sections collapse to a single preamble
unit. That is recorded here honestly and is out of scope for this change —
touching the detector to improve these numbers is exactly what this benchmark
forbids.

### Item 1A extraction behavior

`loaders/sec_headings.py` recognizes SEC Item headings structurally. No LLM, no
embeddings, no fuzzy or semantic matching, no browser automation, no JavaScript
execution — and no issuer, accession, filename, or hash rule anywhere.

**Visible text.** `script` / `style` / `noscript` / `template`, HTML comments,
and elements hidden by `hidden`, `display:none`, `visibility:hidden`, or
`aria-hidden` contribute nothing. Text is assembled as a browser lays it out:
inline runs concatenate with **no** separator, block-level elements and `<br>`
introduce one. This matters — filings routinely split a word across adjacent
styled spans, and joining every element with a space corrupts it. Text is then
NFKC-normalized, stripped of non-breaking and zero-width spaces, whitespace-
collapsed, length-bounded, and rejected outright if it carries control
characters. The document is never flattened into one string for a global regex.

**Candidate blocks.** One document-order traversal emits a non-overlapping
stream of visible blocks, each owned by its nearest block-level ancestor, so a
heading nested `td > div > span` is reported once — as the `td` that renders
it. An inline `span` is never a heading on its own.

**Closed heading grammar**, anchored at the start of a block:

```
HEADING := ["PART" WS ROMAN SEP] "ITEM" WS ITEM_ID [SEP TITLE]
ROMAN   ∈ {I, II, III, IV}                                   (closed)
ITEM_ID ∈ {1, 1A, 1B, 1C, 2, 3, 4, 5, 6, 7, 7A, 8, 9,
           9A, 9B, 9C, 10, 11, 12, 13, 14, 15, 16}            (closed)
SEP     ∈ { . : ; , - – — ) ] whitespace }
TITLE   := bounded remainder, ≤ 120 chars
```

Because the designator must *open* the block, a sentence that merely mentions
Item 1A cannot match; `Item 1Alpha` cannot match; `Item 99` is outside the
closed set; and a bare `Risk Factors` with no Item identity is never Item 1A.
Recognized headings are canonicalized and routed through the existing
`ingest.section_key_for` contract.

**Contents and navigation disambiguation.** Filings repeat Item headings in a
contents table, in navigation links, and in running page headers. Each
candidate gets exactly one classification:

| Class | Rule |
|---|---|
| `designator_only` | the block holds the designator with no title — a contents row whose title sits in a sibling cell, or a running page header |
| `navigation` | the block is wholly an anchor, or it sits in a dense run (≥ 6 Item headings separated by < 400 chars) *and* has no substantive content after it |
| `insufficient_content` | titled, but < 1,000 chars of content before the next different Item |
| `substantive` | everything else |

Neither the first nor the last occurrence is privileged. Exactly one
substantive candidate → `extracted`; none → `missing`; more than one →
`ambiguous`. An ambiguous Item 1A is never stamped with a section key, so it
cannot launder into an extraction. Every rejection records a bounded reason
code.

**Section boundary.** From the selected heading, the section ends at the first
of: a titled, non-navigation Item heading whose id succeeds 1A in the closed
sequence, or a Part **strictly later** than the Part in effect. The second half
of that rule is load-bearing — filings repeat `Part I` as a running page header
throughout the body, and accepting it would truncate the section at its first
page break. A repeat of the *same* Item is likewise page furniture, not a
boundary. The boundary heading is excluded from the section, document order and
`chunk_seq` semantics are preserved, and contents text is never captured as
content. With no trustworthy boundary the outcome is `ambiguous`, never
unbounded consumption; a section over the documented 2,000,000-char safety
bound is also `ambiguous`, never silently truncated.

**Versioning.** Parser version `sec_html_item_headings.v2` (v1 was the implicit
heading-tags-only behavior), recorded on every document the SEC path produces
and in `parser_versions.html_parser`. Build records advance to
`real-filing-benchmark.build.v2` for the extraction diagnostics. Source bytes
and all twenty source hashes are unchanged; new section hashes derive only from
newly extracted content. No annotation was invalidated, because none had ever
been verified.

Generic HTML documents are unaffected: with no substantive Item heading, the
loader uses the pre-existing `h1`/`h2`/`h3` path exactly as before.

---

## Extraction holdout corpus (`real_filing_holdout_v1`) — frozen, METADATA-ONLY

`benchmarks/real_filing_holdout_v1/` holds the frozen holdout manifest
(`real-filing-holdout.manifest.v1`, status **`holdout_frozen_metadata_only`**)
and its selection audit report. It exists because `real_filings_v1` is
development data: its filings were inspected while `sec_html_item_headings.v2`
was designed, so only a corpus whose exact filings were frozen *after* the
parser was frozen and *before* anyone looked at their bodies can ever say
anything about generalization.

**What `holdout_frozen_metadata_only` means, exactly:**

- Exact issuers and exact filing pairs are frozen: 10 issuer pairs, 20 primary
  10-K filings, two consecutive annual 10-Ks per issuer, CIKs, accession
  numbers, filing dates, reporting periods, and primary-document filenames all
  resolved from official SEC metadata.
- **No filing body has been downloaded or inspected.** `expected_sha256` is
  null on every side and `source_verified` is false everywhere — no bytes
  exist, so no checksum can. The gitignored corpus directory for this
  benchmark does not exist yet.
- Nothing has been extracted, compared, packeted, or annotated. The selection
  report's counters say so: `filing_body_requests = 0`,
  `source_documents_downloaded = 0`, `extraction_runs = 0`,
  `comparison_runs = 0`, `annotation_packets = 0`,
  `human_verified_labels = 0`.
- `corpus_role = extraction_holdout_corpus`,
  `extraction_parser_developed_using_this_corpus = false`,
  `extraction_holdout_evaluation = false`, and
  `generalization_claim_supported = false`. The last two stay false until
  bodies are acquired, extraction v2 runs unchanged, and a human verifies
  labels. Being *eligible* to support an out-of-sample claim is not the claim.

**Metadata-only selection protocol (`real-filing-holdout-selection.v1`).**
Predeclared in `real_filing_holdout.selection_protocol()` and frozen by hash
into the manifest before any candidate was resolved:

- **Universe:** unique CIKs from the official `company_tickers.json`
  registrant list, ordered by normalized CIK ascending, then normalized legal
  issuer name (a declared formality — CIKs are unique).
- **Strata:** five closed, non-overlapping SIC ranges, two issuers each —
  `sic-2000s` [2000–2999], `sic-3000s` [3000–3999], `sic-4000s` [4000–4999],
  `sic-5000s` [5000–5999], `sic-6000s` [6000–6999].
- **Target filings:** the issuer's FY2023 and FY2024 annual 10-Ks, where the
  fiscal year is the **filer's own XBRL designation** (`fy`/`fp` on official
  companyfacts fact rows), never a period-end-year heuristic. Exactly one
  original form `10-K` per target year; `10-K/A` amendments can never match;
  `20-F`/`40-F` are never substituted.
- **Exclusions, in declared order:** every development-corpus CIK, every
  development-corpus accession (both derived from the committed
  `real_filing_v1` manifest, not retyped), missing SIC, SIC outside the
  declared strata, unresolvable or ambiguous fiscal-year metadata, missing or
  incomplete filing rows (paged submissions history is read when needed; an
  unreadable page excludes the issuer rather than truncating its history),
  inconsistent chronology, and duplicates of any already-selected issuer,
  CIK, accession, or primary document.
- **Fallback, predeclared:** deferred candidates (whose stratum was already
  full) absorb unfillable slots in original universe order. If ten pairs
  cannot fill, or fewer than five distinct strata result, the selection
  **fails** with a committed failure report — the corpus is never silently
  smaller and the protocol is never altered after partial resolution.
- **Body access is structurally impossible during selection:** every URL must
  match a closed allowlist (`company_tickers.json`, `submissions/CIK*.json`
  and its pages, `api/xbrl/companyfacts/CIK*.json`). A
  `www.sec.gov/Archives/...` primary-document URL has no matching pattern and
  is refused before any transport is consulted. Tests prove the selection
  contacts only declared metadata endpoints.

**The frozen parser is part of the freeze.** The manifest records
`frozen_extraction_parser_version = sec_html_item_headings.v2` and the SHA-256
of `loaders/sec_headings.py` at freeze time, and a test recomputes that hash on
every CI run. **No issuer may be replaced after any future body observation,
and modifying parser v2 in response to holdout results converts the holdout
into development data** — the pinned hash makes that conversion detectable,
at which point a fresh holdout would be required.

**What happens next, in order:** a later, separate acquisition step downloads
the twenty bodies from official sources and records real digests
(`holdout_frozen_metadata_only → source_verified`); extraction v2 runs
**unchanged**; humans annotate; the gold evaluator scores. Only after all of
that could `extraction_holdout_evaluation` become true.

Selection command (the only new networked command; metadata endpoints only):

```bash
export SEC_USER_AGENT="Your Name your.email@your.org"
python scripts/select_real_filing_holdout.py --allow-network
```

**Stage 3 remains current. Stage 3.5 remains in progress** — the holdout is
frozen but not acquired, not extracted, not annotated, and not evaluated.

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
**synthetic comparison regression suite remains the deterministic gate**. Its
offline benchmark step covers manifest and annotation schemas, mocked-HTTP
acquisition, corpus build over tiny synthetic HTML fixtures, SEC-style Item
heading extraction (`tests/test_sec_html_item_extraction.py`), evaluator
refusal/metric behavior, and the metadata-only holdout: deterministic
selection over mocked official metadata
(`tests/test_real_filing_holdout_selection.py`) and the frozen holdout
manifest's schema and denials
(`tests/test_real_filing_holdout_manifest.py`).

The extraction suite is in the required check deliberately: heading recognition
decides *which text is compared*, so a regression there would silently change
every downstream result. Its fixtures are hand-written generic HTML structures
— no real filing content exists in the repository, and CI never rebuilds the
real corpus.

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

## Bounded Chroma upserts (production and benchmark share one helper)

This corpus is what exposed the defect. A single real 10-K produces roughly
5.8k chunks, Chroma refuses one write larger than its client maximum batch size
(5,461 on the installed client), and production `ingest.embed_and_persist` used
to issue that write unbatched — so `python ingest.py` over a real-filing-sized
corpus failed immediately while the four-file committed `docs/` corpus kept the
defect latent.

It is now fixed. Both paths call the same helper,
[chroma_batching.py](chroma_batching.py); the benchmark builder's private
`_add_in_batches` and its 4096 fallback constant are gone, and no ingestion
caller invokes `add_documents` directly.

- **Discovered limit, not a constant.** The helper asks the client for its own
  maximum. A `bool`, a non-integer, a non-positive value, a raising client, or
  a client without the capability fail closed with a stable code instead of
  issuing an unbounded write. The retired 4096 fallback was a guess, and a
  guessed bound would not have been a bound.
- **Deterministic partitioning.** Batch *k* is `items[k*size:(k+1)*size]`. Same
  chunks, same stable ids, same order, same metadata as the unbatched path —
  which is why every build hash, source hash, and section hash in this corpus
  is unchanged by the deduplication.
- **Not a transaction.** If a later batch fails, earlier batches remain in the
  store. What is guaranteed is that the filing registry never *claims* an
  ingestion that did not finish: `chunk_count` is the completion marker, so it
  is cleared before the write and restored only after every batch succeeds.
  Both `ingest.run()` and the benchmark builder follow that order.
- **Explicit rerun is the recovery path.** Deterministic chunk ids upsert over
  whatever landed. Nothing retries, cleans up, or deletes automatically. The
  supported claim is deterministic idempotent upsert on explicit rerun, not
  exactly-once vector insertion.

Coverage is [tests/test_chroma_batching.py](tests/test_chroma_batching.py),
which runs in the required `comparison-regression` check.

---

## Known coverage limitations

- **No accuracy claim exists.** Zero labels are `human_verified`.
- **No generalization claim exists, and this corpus cannot support one.** It is
  an extraction development corpus (`corpus_role =
  extraction_development_corpus`, `generalization_claim_supported = false`).
  Extraction numbers over it are in-sample. A separately frozen holdout corpus
  is required.
- **The holdout corpus is frozen but metadata-only.** `real_filing_holdout_v1`
  freezes exact issuers and filing pairs, but no body has been acquired, no
  checksum exists, no extraction has run over it, and it has produced no
  result of any kind. It cannot support any claim until it is acquired,
  extracted unchanged, and human-annotated.
- The holdout universe is the official registrant ticker list, scanned in
  ascending-CIK order — a deterministic rule that skews the selection toward
  long-registered issuers. It is controlled, not representative, exactly like
  the development corpus.
- Item 1A only. No other SEC section is extracted or compared.
- 10-K only; amendments excluded from v1.
- 10 pairs across 10 large-cap issuers. Small, controlled, and not
  representative of smaller registrants, foreign private issuers, or unusual
  filing formats.
- **No inter-annotator agreement.** One annotator per pair, so no agreement
  statistic exists and none is reported.
- Extraction requires a **titled** Item heading in a single block. A filing
  whose body heading puts the designator and title in separate cells, or that
  answers Item 1A with a short cross-reference (a smaller reporting company may
  write "Not applicable"), records `missing` under the 1,000-char substantive
  floor. That is an honest outcome, not a silent one — the reason code says so.
- Two equally substantive Item 1A headings record `ambiguous` and are never
  resolved by preference. So does a section with no trustworthy end boundary.
- **Unit granularity is coarse on real filings.** The detector treats a line as
  a risk-factor heading only when it ends in `Risk`/`Risks`; most real filings
  use sentence-style headings, so most sections reduce to one preamble unit and
  the change counts are correspondingly coarse. This is a detector limitation,
  it is not addressed here, and it must not be "fixed" by tuning against these
  pairs.
- The heading grammar covers 10-K Item designators only. Other form types and
  other sections are out of scope.
- The detector remains exact heading/content alignment with no similarity
  stage; a reworded-and-retitled risk factor reports as added + removed.
- Direction consistency scores a validator verdict, not a detector-emitted
  direction.
- Acquisition is single-process, single-node, and best-effort against a public
  service. It is not a data-ingestion service.
