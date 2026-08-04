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

## Current status: a holdout gold evaluation exists; generalization remains unsigned

Two different claims, kept apart throughout this document:

1. **A human-verified real-filing gold evaluation now exists** — over the
   *holdout* corpus (`real_filing_holdout_v1`), committed at
   [`benchmarks/real_filing_holdout_v1/gold_evaluation_report.json`](benchmarks/real_filing_holdout_v1/gold_evaluation_report.json),
   which is the **source of truth for every metric**. The source-anchored
   narrative and error analysis is [HOLDOUT_EVALUATION.md](HOLDOUT_EVALUATION.md).
2. **`generalization_claim_supported` is `false`** — and the reason is the
   absence of an explicit admitted generalization sign-off, *not* the absence
   of human labels or of an evaluation. See
   [Generalization sign-off](#7-generalization-sign-off-not-performed).

The **development** corpus (`real_filing_v1`) is a separate corpus and is still
unannotated; the table below describes it unless a row names the holdout.

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
| Human annotation (development corpus) | **not done** — zero labels are `human_verified` on this development corpus; 10 machine-proposed packets exist |
| Gold evaluation | **not done, and the evaluator refuses to produce one** |
| Extraction holdout corpus | **frozen, source-verified, blind-extracted, human-annotated, and gold-evaluated** (`benchmarks/real_filing_holdout_v1/`, manifest status still `corpus_built`) — the frozen parser ran once, unchanged, over all 20 verified bodies: **18/20 sides extracted, 2 ambiguous** (both sides of one pair), 9/10 pairs reached `detected`; those 9 pairs are now `human_verified` and scored. See the holdout section below |
| Holdout gold evaluation | **done** — 9 of 10 pairs scored, 28 human-verified labels, metrics in `gold_evaluation_report.json`, writeup in [HOLDOUT_EVALUATION.md](HOLDOUT_EVALUATION.md) |
| Holdout generalization sign-off | **not done** — `generalization_claim_supported` is `false` because no sign-off exists, not because labels or an evaluation are missing |
| v3 extraction holdout (source-verified) | **frozen + acquired** (`benchmarks/real_filing_v3_holdout_v1/`, status `source_verified`) — 10 new issuer pairs / 20 FY2024→FY2025 10-Ks selected from official SEC metadata only, after parser v3 and evaluation contract v2 were frozen; both prior corpora excluded by CIK and accession. The twenty frozen bodies have since been downloaded from the official SEC archive and checksum-verified; their SHA-256 values are frozen in the manifest and the bodies themselves are not committed. No v3 extraction, comparison, annotation, or evaluation has run. See the v3 holdout section below |
| Unit-segmentation grammar v3 | **implemented, unevaluated** — `item1a_detector.v3` / `comparison_workflow.v3` add the generic heading classes the v2 evaluation showed were missing; the v2 evidence is frozen and byte-identical, the old holdout is now v3 *development* data, and **no v3 evaluation, holdout, or generalization claim exists** (see [Unit grammar v3](#unit-grammar-v3)) |
| Gold-evaluation contract v2 | **frozen, unused on real data** — `real-filing-benchmark.evaluation.v2` + `real-filing-benchmark-metrics.v2` fix the evaluator defect the v2 holdout exposed: future v3 evaluations match by exact canonical `side:sequence:unit_key` subject identity, never by normalized `unit_key`; the frozen contract-v1 evaluation is unchanged and neither contract accepts the other's artifacts (see [Gold-evaluation contract v2](#gold-evaluation-contract-v2)) |

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
4. ~~Source-verify the holdout bodies.~~ **Done** — all twenty frozen primary
   documents were acquired from official SEC sources and checksum-verified
   over decoded bytes; the holdout manifest advanced one step to
   `source_verified` (documented below). The parser has not run over them.
5. ~~Run the blind extraction on that holdout without changing extraction
   v2.~~ **Done** — the frozen parser ran exactly once, unchanged, over all
   twenty verified holdout sides: 18/20 extracted, 2 ambiguous (both sides of
   one pair, preserved as observed); 9/10 pairs reached `detected` and have
   machine-proposed packets; the holdout manifest advanced one step to
   `corpus_built` (documented below).
6. ~~Human annotation and gold evaluation of the holdout, with extraction v2
   still unchanged.~~ **Done** — the nine review-ready pairs were reviewed and
   admitted as `human_verified`, and the gold evaluator scored them with
   extraction v2 unchanged
   (`benchmarks/real_filing_holdout_v1/gold_evaluation_report.json`,
   writeup in [HOLDOUT_EVALUATION.md](HOLDOUT_EVALUATION.md)). Extraction was
   **not** modified in response to the results.
7. **An explicit generalization sign-off, if one is ever to be made.** The
   evaluation is complete and unsigned:
   `generalization_claim_supported` is `false` because no sign-off exists.
   Human-verified labels and evaluator completion are **not** a sign-off.
8. **Version the unit-segmentation grammar as v3.** ~~Pending~~ **Done** —
   the defect the evaluation exposed was in *unit segmentation*
   (`comparison_detector`'s heading grammar), not in SEC Item 1A section
   extraction (`sec_html_item_headings.v2`, which is unchanged). The unit
   grammar is now versioned and advanced: `item1a_detector.v3` /
   `comparison_workflow.v3` recognize the generic heading classes the v2
   suffix rule could not express (see [Unit grammar v3](#unit-grammar-v3)
   below). The v2 evaluation stays frozen under its v2 identity, and the
   current holdout is now **v3 development data** — its failure modes were
   observed, so it can never serve as unseen v3 evidence.
9. **Freeze the v3 gold-evaluation contract.** ~~Pending~~ **Done** — the v2
   holdout also exposed an *evaluator* defect, separate from unit
   segmentation: gold matching and metric bookkeeping keyed subjects by the
   normalized `unit_key`, which collapses repeated normalized headings the
   v3 representation deliberately preserves. The future evaluation contract
   is now explicitly versioned (`real-filing-benchmark.evaluation.v2` +
   `real-filing-benchmark-metrics.v2`) and matches by exact canonical unit
   identity (see [Gold-evaluation contract v2](#gold-evaluation-contract-v2)
   below). No v2 metric was recomputed and no v2 label was migrated.
10. **A newly frozen unseen holdout for v3.** ~~Pending~~ **Metadata-only
   selection done** — deliberately in a separate commit after both parser v3
   and the v2 evaluation contract were merged and frozen:
   `real_filing_v3_holdout_v1` (see [v3 extraction
   holdout](#v3-extraction-holdout-real_filing_v3_holdout_v1--source-verified-unextracted)
   below) freezes 10 new issuer pairs / 20 FY2024→FY2025 10-Ks from official
   SEC metadata only, under a hash-ranked deterministic protocol that
   excludes every CIK and accession of both prior corpora. Filing bodies
   were not downloaded or inspected at selection time. **Source acquisition
   is now done too**: the same twenty frozen documents were downloaded from
   the official SEC archive and checksum-verified, advancing the manifest one
   step to `source_verified` without running extraction. Until the corpus is
   blind-extracted, annotated, and evaluated, **no v3 accuracy or
   generalization claim exists**.

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
- Machine annotation is **not gold**. Zero labels are `human_verified` **on
  this development corpus**, so no accuracy claim exists for it and none can
  until human review. (The separate *holdout* corpus has since been
  human-annotated and gold-evaluated; see below.)

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
one: at the recorded `item1a_detector.v2`, `comparison_detector._HEADING_RE`
only treated a line as a risk-factor heading when it ends in `Risk`/`Risks`,
and most real filings write sentence-style risk headings, so those sections
collapse to a single preamble unit. That is recorded here honestly and was out
of scope for this change — touching the detector to improve these numbers is
exactly what this benchmark forbids. (The unit grammar has since been
versioned to `item1a_detector.v3` in a new development cycle — see
[Unit grammar v3](#unit-grammar-v3) — without altering any number recorded
here.)

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

## Extraction holdout corpus (`real_filing_holdout_v1`) — frozen, source-verified, blind-extracted, GOLD-EVALUATED

`benchmarks/real_filing_holdout_v1/` holds the frozen holdout manifest
(`real-filing-holdout.manifest.v1`, now status **`corpus_built`**), its
selection audit report, its source-verification report, and the three
blind-extraction artifacts (`blind_extraction_report.json`,
`execution_report.json`, `annotation_packet_inventory.json`). It exists because
`real_filings_v1` is development data: its filings were inspected while
`sec_html_item_headings.v2` was designed, so only a corpus whose exact filings
were frozen *after* the parser was frozen and *before* anyone looked at their
bodies can ever say anything about generalization.

**The freeze (`holdout_frozen_metadata_only`, unchanged since selection):**

- Exact issuers and exact filing pairs are frozen: 10 issuer pairs, 20 primary
  10-K filings, two consecutive annual 10-Ks per issuer, CIKs, accession
  numbers, filing dates, reporting periods, and primary-document filenames all
  resolved from official SEC metadata. At freeze time no filing body had been
  downloaded or inspected; `expected_sha256` was null on every side. The
  selection report preserves that state (`filing_body_requests = 0`) and
  records the SHA-256 of the metadata-only manifest bytes.
- `corpus_role = extraction_holdout_corpus`,
  `extraction_parser_developed_using_this_corpus = false`,
  `extraction_holdout_evaluation = false`, and
  `generalization_claim_supported = false`. The last two stay false until
  extraction v2 runs unchanged and a human verifies labels. Being *eligible*
  to support an out-of-sample claim is not the claim.

**What `source_verified` adds — and all it adds:**

- The exact twenty frozen primary documents were downloaded from their
  canonical official EDGAR URLs (`https://www.sec.gov/Archives/...`, derived
  from the frozen CIK/accession/document fields, host `www.sec.gov` only) by
  `scripts/acquire_real_filing_holdout.py`, using the existing acquisition
  transport: validated `SEC_USER_AGENT`, paced requests, bounded transport
  retry with a transient-status allowlist, `Retry-After` honored and capped,
  and a redirect guard that refuses any redirect target off the official-host
  allowlist.
- Each SHA-256 is computed over the **decoded** entity bytes (never a gzip
  transport container), written atomically, and confirmed by re-reading the
  cached file before the digest is accepted. The checksums proved reproducible
  across two independent downloads of all twenty documents.
- The manifest advanced exactly one step
  (`holdout_frozen_metadata_only → source_verified`): per-side
  `expected_sha256` filled with the verified digests, per-side
  `source_verified` set true, and nothing else — same pairs, same parser
  version and parser-source hash, same selection-protocol hash, same
  development-corpus exclusions. The source-verification report links the
  metadata-only manifest hash to the new one, so the chain from freeze to
  verification is checkable forever.
- Filing bodies live only under the gitignored
  `benchmark_data/real_filing_holdout_v1/` tree. A verified cached file is
  reused instead of re-fetched; a cached file that disagrees with a recorded
  digest is preserved and refused, never overwritten. If any one of the twenty
  sides fails, the manifest does **not** advance, the pair is **not**
  replaced, and a bounded failed report is written locally only; an explicit
  rerun reuses verified cache and continues.
- At source verification the parser had not run over these filings: the
  acquisition module's import graph excludes `loaders`, `ingest`, Chroma, and
  the comparison detector (a test pins this), and the parser source is read
  only as bytes to re-verify its frozen hash. The blind extraction below was
  a later, separate step.

**What `corpus_built` adds — the blind extraction run:**

- `scripts/run_real_filing_holdout_blind_extraction.py` (offline; no network
  code is reachable from its import graph) ran the frozen parser **exactly
  once, unchanged**, over all twenty verified bodies, in one predeclared
  execution: refuse on any frozen-identity drift (parser bytes, exclusions,
  manifest hash chain), hash every frozen code file, re-verify all twenty
  source checksums, run the EXISTING ingestion + extraction + comparison path
  per pair in manifest order, recompute every frozen code hash, and require
  exact equality. `blind_extraction_report.json` records the before/after
  hashes (`frozen_code_unchanged: true`) and one bounded row per side.
- **The blind result, preserved exactly as observed: 18/20 sides
  `extracted`, 2 `ambiguous`, 0 `missing`, 0 `parse_failed`.** Both sides of
  one pair (`sic-6000s-01`) are ambiguous: the parser itself found exactly
  one substantive Item 1A heading (`single_substantive_item_heading`), but
  the section key landed on two non-contiguous chunk runs, and which run is
  "the" section is not deterministically decidable, so the frozen rules
  refuse to guess. That pair **stays in the corpus, blocked and unrepaired**
  — fixing it would require a parser change, which would convert this
  holdout into development data, require freezing a parser v3, and require
  selecting a NEW unseen holdout.
- The existing comparison workflow ran for exactly the 9 fully extracted
  pairs (never for the blocked pair — no detection attempt exists for it);
  all 9 reached `detected`. Machine-proposed annotation packets were
  generated for those 9 pairs only; **at the blind-extraction commit** every
  annotation was `machine_proposed` with a null annotator, and the committed
  inventory records packet hashes, section hashes, unit counts, and the one
  blocked pair with its reason. Those nine have since been human-reviewed and
  admitted as `human_verified` — see the gold evaluation below.
- The manifest advanced exactly one step (`source_verified → corpus_built`):
  status, role prose, and description — same pairs, same digests, same parser
  version and parser-source hash, same protocol hash, same exclusions. The
  blind-extraction report chains the source-verified manifest hash to the new
  one, extending the freeze → verification → blind-run chain.
- **What the blind-extraction artifacts may claim: extraction coverage only.**
  Exact extracted / missing / ambiguous / parse-failed counts, the buildable
  pair count, and packet availability. They claim no detector accuracy, no
  annotation accuracy, no precision or recall of any kind, and no
  generalization of detector quality. Coverage is not correctness, and those
  three reports carry `extraction_holdout_evaluation = false` because **at
  that commit** no label was `human_verified`. Accuracy numbers come only
  from the gold evaluation below, whose report carries
  `extraction_holdout_evaluation = true`.

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

**That review and evaluation have since happened.** Humans reviewed the nine
machine-proposed packets and produced `human_verified` annotations; the gold
evaluator scored them with extraction v2 still unchanged, and
`extraction_holdout_evaluation` is now `true` in the resulting report. See
[What the gold evaluation found](#what-the-gold-evaluation-found) below. What
has *not* happened is a generalization sign-off.

**Human-review validation (`scripts/validate_holdout_human_annotations.py`).**
The admission gate between human review and gold evaluation, offline and
strictly read-only in both modes:

- `--workspace` validates pre-review integrity: the **nine review-ready
  pairs** (`sic-2000s-01/02`, `sic-3000s-01/02`, `sic-4000s-01/02`,
  `sic-5000s-01/02`, `sic-6000s-02`) must match their committed inventory
  packet hashes and bind the recorded source checksums, section hashes,
  result hashes, parser/detector/workflow versions, and the manifest hash
  chain; the **one extraction-blocked pair** (`sic-6000s-01`,
  `extraction_ambiguous`) must have no packet, no template, and no annotation
  file at all.
- The default (completed) mode additionally requires, for every review-ready
  pair, a completed annotation that is **explicitly `human_verified`** with a
  bounded annotator id, an explicit-UTC verification timestamp postdating
  packet generation, canonical label ids, **exactly-once closure over every
  previous and current unit id** (a filing that repeats a normalized heading
  needs an explicit label per unit id — key-level coverage is not closure),
  and bounded notes carrying no filing excerpts, no absolute paths, and no
  credential material.
- The empty human-completion templates under
  `benchmark_data/real_filing_holdout_v1/annotations/<pair_id>.json` are
  **local preparation artifacts, not annotations**: every decision field is
  null, and the validator treats them as "not completed", never as labels.
- Only explicitly completed `human_verified` files may enter the gold corpus.
  **Validator acceptance is necessary but does not itself establish label
  correctness** — it proves identity, closure, and hygiene, not that a
  human's judgement is right. The blocked pair is excluded from every
  count: never detector-correct, detector-incorrect, unchanged, or
  annotation-missing.
- The validator computes no metric — it is an admission gate, not an
  evaluator. The contract is frozen by
  `tests/test_holdout_human_annotation_validation.py` in the required
  merge-blocking CI check, entirely over synthetic fixtures. All nine
  review-ready pairs have since passed this gate and been admitted as
  `human_verified`.

### What the gold evaluation found

The nine admitted pairs were scored by `scripts/eval_real_filing_benchmark.py`
with extraction v2 unchanged. **The committed report
[`benchmarks/real_filing_holdout_v1/gold_evaluation_report.json`](benchmarks/real_filing_holdout_v1/gold_evaluation_report.json)
is the source of truth for every metric**; the source-anchored narrative and
error analysis is [HOLDOUT_EVALUATION.md](HOLDOUT_EVALUATION.md). The numbers
are not repeated here — one authoritative copy, plus the writeup.

**Annotation and scoring state** (`$.corpus_quality`, `$.scoring_scope`,
`$.label_statistics`):

- 9 of the 10 frozen pairs were scored (`pairs_scored: 9`,
  `pairs_in_manifest: 10`, `coverage_complete: false`).
- The one extraction-blocked pair (`sic-6000s-01`, ambiguous on both sides) is
  recorded as an explicit exclusion with code `extraction_blocked` and is
  **never silently included in a detector-quality denominator** — every gold
  numerator and denominator counts the scored pairs only.
- 28 labels are `human_verified` across those nine pairs
  (`human_verified_label_count: 28`), by a single annotator.
- The report declares `extraction_holdout_evaluation: true`, `corpus_role:
  extraction_holdout_corpus`, `detector_version: item1a_detector.v2`,
  `workflow_version: comparison_workflow.v2`, and `manifest_status:
  corpus_built` — the evaluation did **not** advance the manifest.

**What these metrics are, and what they are not.** They are real out-of-sample
holdout metrics **at the frozen v2 unit granularity** — the unit boundaries the
system under test produced. They are **not** risk-factor-item-level accuracy,
because the frozen segmentation frequently did not operate at that granularity:
`comparison_detector._HEADING_RE` treats a line as a risk heading only when it
ends in `Risk`/`Risks`, so many sections collapse toward a single preamble unit
and some pairs are scored over units that span the whole Item 1A section.
Describing these figures as per-risk-factor accuracy would misstate what was
measured. HOLDOUT_EVALUATION.md counts the affected headings and pairs against
their sources.

**The principal observed limitation is segmentation and unit-boundary quality,
not evidence resolution** — evidence references resolved at
`$.gold_metrics.evidence_resolution_rate` while the change-level metrics did
not, so the weakness is in *where the units were cut*, not in whether evidence
pointed at the right indexed chunks. The labelling was also not blind (the
packet shows the machine proposal before the reviewer decides), and there is a
single annotator with no agreement statistic.

**The parser was not changed in response — and when the unit grammar later
was, it advanced under a new version.** These failures had already been
observed on this holdout, so any fix converts it into development data and
makes any subsequent number on it post-hoc. The v3 development cycle
([Unit grammar v3](#unit-grammar-v3)) therefore treats this corpus as
development/diagnostic data only, leaves every artifact above byte-identical,
and requires a newly frozen unseen holdout before any v3 evaluation exists.
The live workflow now *refuses* to gold-score this corpus at all: its config
declares `item1a_detector.v2` / `comparison_workflow.v2`, and the evaluator's
version gate refuses the mismatch rather than recomputing metrics under an
identity the config does not describe.

**`generalization_claim_supported` is `false`** (`$.generalization_claim_supported`),
and `$.generalization_claim.signoff_present` is `false`. The claim is blocked
by the absence of an explicit admitted sign-off — not by the absence of human
labels, not by the metric values, and not by coverage. Human-verified labels
and evaluator completion are **not** themselves a generalization sign-off. The
contract is in [Generalization sign-off](#7-generalization-sign-off-not-performed).

Holdout commands (the first two are networked, operator-run, never in CI, and
require a descriptive `SEC_USER_AGENT`; the rest are offline and never in CI
either — CI validates the committed artifacts and never rebuilds the real
corpus):

```bash
export SEC_USER_AGENT="Your Name your.email@your.org"
python scripts/select_real_filing_holdout.py --allow-network    # done: the freeze (metadata only)
python scripts/acquire_real_filing_holdout.py --allow-network   # done: source verification (bodies + checksums)
python scripts/run_real_filing_holdout_blind_extraction.py      # done: the blind extraction run (offline)
python scripts/validate_holdout_human_annotations.py --workspace  # pre-review integrity (read-only)
python scripts/validate_holdout_human_annotations.py              # after human review: gold-admission check

# v3 holdout (same discipline, separate corpus):
python scripts/select_real_filing_v3_holdout.py --allow-network   # done: the freeze (metadata only)
python scripts/acquire_real_filing_v3_holdout.py --allow-network  # done: source verification (bodies + checksums)
```

**Stage 3 remains current. Stage 3.5 remains in progress** — the holdout is
frozen, source-verified, blind-extracted, human-annotated on its nine
review-ready pairs, and gold-evaluated at the v2 unit granularity, with no
generalization sign-off. The SEC HTML extraction parser is unchanged: its
source still hashes to the digest frozen before any body was seen, and the two
ambiguous sides are preserved rather than repaired. The *unit grammar* has
since advanced to v3 as a new development cycle (see
[Unit grammar v3](#unit-grammar-v3)); no v3 evaluation of any kind exists.

---

## Unit grammar v3

The v2 gold evaluation exposed a *unit-segmentation* limitation — the
detector's heading grammar, not SEC Item 1A section extraction
(`sec_html_item_headings.v2` is byte-identical and stays frozen). Unit
segmentation is owned by `comparison_detector` and is now explicitly
versioned there:

- **Identities.** `item1a_detector.v3` / `comparison_workflow.v3` (the
  workflow version is part of the comparison identity key, so the same filing
  pair re-compares as a *new* comparison; stored v2 results are never
  overwritten, and stale replay still answers `detector_version_superseded`).
  Internally the heading grammar is dispatched by version: `item1a_units.v2`
  (frozen, byte-identical to the grammar behind every committed v2 artifact,
  callable via `extract_units(grammar_version=...)`) and `item1a_units.v3`
  (the default). An unknown grammar version is refused with a stable code.
- **Generic grammar classes added in v3** — closed, deterministic, line-
  anchored regex classes; no LLM, no embeddings, no fuzzy matching, no
  issuer/CIK/accession/filename/hash rule, and CI tests assert that absence:
  1. the existing v2 suffix form (`... Risk` / `... Risks`), unchanged;
  2. prefix category headings — `Risks Related to ...` / `Risk Related to ...`
     / `Risks Relating to ...` with a capitalized connector (lowercase
     "risks related to ..." is prose and stays rejected);
  3. the `General Risk Factors` / `General Risk Factor` category heading;
  4. `/` added to the closed punctuation set (compound category headings);
     sentence terminals stay outside every class, so prose keeps its period
     and fails, and length bounds are enforced exactly.
- **Repeated normalized headings never collapse.** Two units whose headings
  normalize to the same key keep distinct canonical sequence-aware identities
  (`side:sequence:unit_key`). Ambiguous duplicate-heading units serialize one
  undetermined change **per unit** (v2 collapsed them to one change per key),
  annotation packets emit one row and one machine label per occurrence bound
  to its own unit id, and the synthetic regression labels match those changes
  by identity. The unit id, not the normalized heading, is the primary key at
  every seam.
- **The v2 evidence is untouched.** Every committed v2 artifact — the holdout
  manifest, the nine human-verified annotations (28 labels), the gold
  evaluation report and its metrics, both evaluation configs, all
  blind-extraction and verification reports, and HOLDOUT_EVALUATION.md — is
  byte-identical and keeps its recorded v2 identity. The live evaluator now
  *refuses* to gold-score either frozen corpus (version gate), so the v2
  numbers cannot be silently recomputed under v3.
- **v2 and v3 unit labels are not comparable.** v3 changes unit boundaries
  and unit identities, so the label universe differs; no v2 label-level
  metric may be set beside a future v3 metric as though the units were the
  same thing.
- **The old holdout is v3 development data.** Its failure modes were observed
  before v3 was written, so it can never serve as unseen v3 evidence. Local
  v3 diagnostics over it are structural only (unit counts, recognized
  headings, repeated-heading preservation), gitignored, and are **not** a
  holdout evaluation.
- **What does not exist yet:** no v3 gold evaluation, no v3 accuracy number,
  no generalization claim (`generalization_claim_supported` remains `false`
  and unsigned). The metadata-only v3 holdout selection is now frozen
  (`real_filing_v3_holdout_v1`, selected after v3 was merged and frozen);
  its bodies remain deliberately unfetched and unevaluated.

Tests: `tests/test_item1a_unit_parser_v3.py` (generic synthetic fixtures for
every positive and negative grammar class, determinism, sequence-aware
identity, v2 byte-compatibility, packet-seam preservation, and CI pinning),
plus the updated detector/regression suites — all in the required
`comparison-regression` check.

---

## Gold-evaluation contract v2

The v2 holdout exposed a second, separate defect — in the *evaluator*, not
the detector: gold matching, duplicate handling, and pair exact match keyed
every subject by the normalized `unit_key`, silently collapsing units whose
headings normalize identically, even though the v3 detector, the packet
generator, and the annotation admission validator all preserve each occurrence
under the canonical sequence-aware identity `side:sequence:unit_key`. Changed
matching semantics may not hide behind an unchanged version string, so the
evaluation contract is now explicit and versioned in
`scripts/eval_real_filing_benchmark.py`:

- **Two closed contracts, dispatched from the config alone.** Contract v1
  (`real-filing-benchmark.evaluation.v1` + `real-filing-benchmark-metrics.v1`)
  is the frozen historical semantics behind the committed v2-detector
  evaluations; it stays readable and identifiable and scores
  `item1a_detector.v2` / `comparison_workflow.v2` artifacts only. Contract v2
  (`real-filing-benchmark.evaluation.v2` + `real-filing-benchmark-metrics.v2`,
  reports `real-filing-benchmark.report.v2`) is required for
  `item1a_detector.v3` / `comparison_workflow.v3` artifacts and additionally
  requires the config to declare `declared_unit_grammar_version:
  item1a_units.v3`. Unknown or mixed version pairings fail closed
  (`evaluation_contract_version_unknown`); nothing is inferred from whichever
  report happens to exist, and no legacy config is silently upgraded. Neither
  contract accepts the other's artifacts
  (`evaluation_contract_incompatible_detector` / `_workflow` /
  `_unit_identity`).
- **Canonical subjects, not normalized headings.** A contract-v2 subject is
  the exact ordered pair of canonical unit identities a change or label binds
  (`added` = current only, `removed` = previous only, `modified`/`unchanged` =
  both, `undetermined` = the labelled combination). Matching is exact
  subject-key equality, then change type: no fuzzy matching, no heading
  similarity, no `unit_key` fallback, no order dependence. One prediction
  satisfies at most one gold subject and the reverse. `unit_key` remains
  descriptive metadata inside the identity.
- **Fail-closed subject validation before any metric.** Unknown, missing,
  wrong-side, wrong-sequence, or metadata-drifted identities refuse with
  stable codes (`gold_subject_*`, `prediction_subject_*`,
  `evaluation_subject_shape_invalid`); duplicate canonical subjects are an
  invalid state, never silently deduplicated; and gold labels must close over
  the built unit inventory exactly once per identity
  (`evaluation_unit_inventory_not_closed`). Repeated normalized headings
  therefore stay separate subjects in precision and recall denominators,
  type accuracy, unchanged false-positive counting, and pair exact match —
  which now compares complete canonical subject/type sets.
- **The v2 evidence is untouched.** Formulas, metric names, zero-denominator
  `null` policy, blocked-pair exclusion, and the sign-off gate are unchanged;
  the identity is the correction. The committed contract-v1 configs and the
  frozen `gold_evaluation_report.json` keep their recorded identities
  byte-identical, no v2 metric was recomputed, and no v2 annotation was
  migrated to canonical v3 subjects — the two label universes are not
  comparable because the unit definitions differ.
- **What still does not exist:** no v3 human annotation, no v3 gold
  evaluation, no v3 accuracy number, and no generalization claim
  (`generalization_claim_supported` remains `false` and unsigned).
  Evaluator-contract correctness is not detector correctness. The
  metadata-only v3 holdout (`real_filing_v3_holdout_v1`) is now frozen under
  this contract; its bodies remain deliberately unfetched.

Tests: `tests/test_v3_gold_evaluator_contract.py` (synthetic-only: contract
dispatch and cross-acceptance refusals, canonical identity validation,
repeated-heading occurrence preservation across every metric, duplicate and
closure refusal, determinism, order independence, and CI pinning) — in the
required `comparison-regression` check.

---

## v3 extraction holdout (`real_filing_v3_holdout_v1`) — SOURCE-VERIFIED, unextracted

`benchmarks/real_filing_v3_holdout_v1/` holds the second frozen holdout
manifest (`real-filing-v3-holdout.manifest.v1`, now at status
`source_verified`), its bounded selection audit report, a bounded
source-verification report, and a future evaluation config. The *selection*
was committed **before** any selected filing body was downloaded or
inspected; the bodies were acquired afterwards, and the selection has not
changed since. Both prior corpora are spent as v3 evidence: the
development corpus by construction, and `real_filing_holdout_v1` because its
failure modes were observed and then used to design `item1a_units.v3` and the
contract-v2 evaluator, which makes it v3 development data. This corpus is the
one a future v3 generalization claim must be earned on.

- **Selected after both freezes, before any body observation.** The
  selection ran only after `item1a_units.v3` / `item1a_detector.v3` /
  `comparison_workflow.v3` and `real-filing-benchmark.evaluation.v2` +
  `real-filing-benchmark-metrics.v2` + `report.v2` were merged and frozen.
  The manifest pins each of those identities by version and — for
  `loaders/sec_headings.py`, `comparison_detector.py`,
  `comparison_store.py`, and `scripts/eval_real_filing_benchmark.py` — by
  source sha256, so post-freeze drift in any pinned file is a checkable
  fact, and required CI checks it.
- **Deterministic hash-ranked selection under a fixed seed**
  (`real-filing-v3-holdout-selection.v1`, hash-frozen into the manifest):
  candidates from the official `company_tickers.json` registrant list are
  ranked by `SHA-256("real_filing_v3_holdout_v1|" + zero-padded CIK)`
  ascending (ties by CIK, then title) instead of the first holdout's
  ascending-CIK order, which had selected only the earliest-registered
  issuers. The seed is the benchmark id, fixed before selection; no runtime
  seed input exists and no randomness is used.
- **Both prior corpora excluded, from their committed manifests.** Every
  CIK and, independently, every accession in `real_filing_v1` and
  `real_filing_holdout_v1` is excluded (10 CIKs + 20 accessions each). The
  exclusion sets are derived at run time — never retyped — and the sha256 of
  each source manifest is frozen into the new manifest; CI re-derives both
  sets and refuses drift.
- **Fixed filer-designated FY2024 → FY2025 target** via official XBRL
  `fy`/`fp` companyfacts rows (never a period-end-year heuristic), two
  consecutive annual 10-Ks per issuer, exact form `10-K` only (no 10-K/A,
  20-F, or 40-F), 10 distinct issuers across the same five closed SIC strata
  (2000s–6000s, two each), under a 500-probe budget. An unfillable stratum
  fails the entire selection; the corpus is never silently smaller and the
  target years are never switched or mixed.
- **Body access was structurally impossible during selection**: every URL
  passed the same closed metadata allowlist as the first holdout (registrant
  list, submissions incl. paged history, companyfacts — no Archives
  pattern), so the *selection* report records `filing_body_requests = 0` by
  construction, and its downstream counters (`source_documents_downloaded`,
  `extraction_runs`, `comparison_runs`, `annotation_packets`,
  `human_verified_labels`, `gold_evaluation_runs`) are all zero.
- **The twenty bodies have since been acquired and source-verified**
  (`holdout_frozen_metadata_only → source_verified`, one lifecycle step,
  protocol `real-filing-v3-source-acquisition.v1`). Each body came from the
  one canonical official archive URL derived from the frozen CIK, accession,
  and primary-document fields — `https://www.sec.gov/Archives/edgar/data/`
  only, over https, exact hostname equality, no query, no fragment, no index
  page, no exhibit, no alternate accession, and an identity-bound redirect
  guard that refuses any target other than that exact URL. SHA-256 is taken
  over the **decoded entity bytes** (Content-Encoding applied first, before
  any text decoding, Unicode normalization, newline normalization, or
  parsing); bytes are written atomically, re-read in binary, and re-hashed,
  and the digest is accepted only when the two agree. All 20 sides verified
  in a single run (20 requests, 0 retries, 0 redirects, 87,281,002 bytes),
  and every `expected_sha256` and `source_verified: true` in the manifest
  comes from that run. **The bodies themselves are not committed** — they
  live only under gitignored `benchmark_data/real_filing_v3_holdout_v1/`.
  The transition is all-or-nothing: one failed side would have left the
  committed manifest metadata-only with every digest still null, and no
  selected filing may be replaced for any reason, including a failed URL,
  an unusual document, or a suspicion that Item 1A is absent.
- **Reproducing the source set later requires the same exact archive bytes.**
  The freeze carried null digests, so this first acquisition observed them
  and froze them (trust-on-first-acquisition, stated in the report); every
  later read verifies against the committed values. A mismatch — remote or
  local — fails closed. A disagreeing local file is preserved and refused,
  never overwritten, never deleted, and never silently re-pinned.
- **Source verification is not parser validation.** It establishes only which
  official bytes were obtained. It does **not** mean extraction succeeded,
  and it is not a claim that all twenty filings contain an extractable
  Item 1A section — that is unknown until the separate blind extraction run
  reports it.
- **The future evaluation config is committed now**, while the corpus is
  metadata-only: it declares contract v2 (`evaluation.v2` + `metrics.v2` +
  `report.v2`, canonical `side:sequence:unit_key` subject matching over
  `item1a_units.v3`), binds to this benchmark id, carries null thresholds
  and a null sign-off, and cannot produce a report — the evaluator has no
  branch that reads this manifest schema, so gold scoring is structurally
  unreachable until later stages exist.
- **Selection is auditable but not byte-replayable from a later snapshot.**
  The rules are pinned by `selection_protocol_hash` and the outcome by the
  manifest hashes, but the repository does not store the SEC metadata
  snapshot the selection read, so a rerun against later metadata may select
  differently; any such rerun is a distinct selection attempt, documented,
  never a silent overwrite.
- **What this corpus is not (yet):** it supports no claim of representative
  sampling and no accuracy statement of any kind.
  `extraction_holdout_evaluation` and `generalization_claim_supported` are
  false; no v3 extraction run, unit-parser v3 execution, comparison,
  annotation packet, machine label, human label, v3 gold evaluation, or
  sign-off exists for it, and the source-verification report records all of
  those counters as zero. The next step is a **blind extraction and
  comparison run** over these already-verified bytes, with the frozen parser,
  unit grammar, detector, workflow, and evaluator contract unchanged.

Tests: `tests/test_v3_holdout_selection.py` (deterministic hash-ranked
selection, fixed seed, dual-corpus exclusions, strata/eligibility denials,
the closed metadata allowlist, zero-counter audit report — all over
synthetic metadata), `tests/test_v3_holdout_manifest.py` (the committed
manifest/config/report of record: schema and denials, live pinning of every
frozen v3/v2 identity and source hash, exclusion provenance, and the
config's contract declarations), `tests/test_v3_holdout_source_acquisition.py`
(URL construction and the closed URL allowlist, redirect refusals,
transport-level response checks, entity-byte hashing, local-file reuse and
mismatch refusal, and the all-or-nothing transition — mocked transport and
synthetic HTML only), and
`tests/test_v3_holdout_source_verification.py` (the committed advanced
manifest and source-verification report: the one-step transition, the
manifest hash chain, twenty distinct verified digests, report/manifest
binding, canonical-URL-only records, prose denials, and no committed body)
— all in the required `comparison-regression` check.

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

The holdout's local corpus mirrors the `sources/` half of this layout under
`benchmark_data/real_filing_holdout_v1/` (same gitignore, same
`acquisition.json` sidecars); it has no `build/`, `packets/`, or
`annotations/` because nothing downstream of bytes-on-disk has run.

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

### 7. Generalization sign-off (not performed)

A completed gold evaluation and a generalization claim are two different
things, and this repository keeps them apart structurally.

`generalization_claim_supported` is **false** for the holdout evaluation of
record, and the reason is not a metric value, a coverage gap, or a corpus role
— it is that **no human has signed it**. Coverage, out-of-sample corpus role,
and human-verified labels are all necessary context for that judgement, and
none of them is the judgement. `evaluate_generalization_claim` is not even
passed a metric, so no number can grant or block the claim.

The claim is gated on `generalization_claim_signoff` in `evaluation_config.json`,
which is `null` in both committed configs. **No tool in this repository writes
it**, and a test asserts the absence of any writer — the same contract as
`human_verified` on an annotation. To be affirmative, a sign-off must carry
*all* of:

| Field | Requirement |
|---|---|
| `signer_id` | Identifies a person; a placeholder (`TBD`, `unknown`, `anonymous`, …) is rejected |
| `signed_at_utc` | ISO-8601 with an explicit UTC offset; a naive timestamp is rejected |
| `manifest_sha256` | Must equal the manifest hash **this run** evaluated |
| `acknowledged_pairs_scored` | Must equal the pair count **this run** scored |
| `statement` | An explicit, non-empty, bounded sentence the signer wrote |

`statement` is required, and that requirement is the strict part. It must be a
`str` — never `null`, a boolean, a number, a list, a mapping, or bytes — and it
must carry visible text: whitespace-only, non-breaking-space-only, and
zero-width-only values are all rejected. Leading and trailing whitespace is
trimmed and the trimmed value is what is persisted, following the repository's
existing convention for bounded human text; interior spacing, punctuation, and
newlines are preserved exactly. The bound is **2,000 Unicode code points**,
unchanged from what the field already carried. Stable error codes:
`generalization_signoff_statement_required`, `..._invalid_type`, `..._empty`,
`..._too_long`.

Three things this deliberately does **not** do:

- **No statement is ever generated.** There is no default, no template, and no
  derivation from the signer id, the timestamp, the annotator, the metrics, the
  command-line user, or the environment. A claim nobody wrote a sentence for is
  a claim nobody made.
- **A statement alone is never sufficient.** Every condition above still
  applies; the statement is additive and can rescue nothing.
- **Verified annotations are not a sign-off.** That the holdout labels are
  `human_verified` by a named annotator, and that the gold evaluator ran to
  completion, are facts about *labels* and *execution*. Neither is a claim
  about generalization, and nothing promotes one into the other — the
  annotation and sign-off schemas share no field.

Validator acceptance means a bounded sentence exists, nothing more. It is not
agreement with any particular claim, and it is **not** an electronic signature,
a cryptographic attestation, or a compliance certification.

An unsigned completed evaluation is a legitimate, permanent state: the
committed report is exactly that, and it remains valid historical evidence with
`generalization_claim_supported = false`.

There is no sign-off command, and this repository does not provide one. The
validator (`validate_signoff_statement`) is the seam a future one must use.
Invalid input is refused before any metric is computed or any file is written —
the CLI exits `2` with a stable code and produces no report, so no partial
affirmative state can survive a rejection.

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
refusal/metric behavior, and the holdout: deterministic selection over mocked
official metadata (`tests/test_real_filing_holdout_selection.py`), the frozen
holdout manifest's schema and denials
(`tests/test_real_filing_holdout_manifest.py`), mocked-transport holdout
acquisition behavior (`tests/test_real_filing_holdout_acquisition.py`), the
committed source-verified artifacts' hash chain and claims
(`tests/test_real_filing_holdout_source_verification.py`), and the
human-annotation admission contract — frozen pair sets, identity and hash
bindings, exactly-once unit closure, read-only behavior
(`tests/test_holdout_human_annotation_validation.py`, entirely over synthetic
fixtures), and the generalization sign-off admission contract — the strict
non-empty bounded `statement`, every gate a statement cannot rescue, and the
committed gold evaluation pinned byte-identical, unsigned, and
`generalization_claim_supported = false`
(`tests/test_gold_evaluation_signoff.py`).

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

- **No accuracy claim exists for the *development* corpus.** Zero of its
  labels are `human_verified`. The *holdout* corpus has been evaluated; see
  the next two entries.
- **No generalization claim exists, and the development corpus cannot support
  one.** It is an extraction development corpus (`corpus_role =
  extraction_development_corpus`, `generalization_claim_supported = false`).
  Extraction numbers over it are in-sample.
- **The holdout evaluation is complete but unsigned.**
  `real_filing_holdout_v1` has been frozen, source-verified, blind-extracted,
  human-annotated, and gold-evaluated, and its report is the source of truth
  for every real-filing metric. `generalization_claim_supported` remains
  `false` because no explicit generalization sign-off has been admitted —
  human-verified labels and evaluator completion are not a sign-off.
- **Holdout metrics are at the frozen v2 unit granularity, not
  risk-factor-item level.** Unit boundaries came from the system under test,
  and the frozen heading rule frequently did not cut at individual risk
  factors. The principal observed limitation is segmentation and unit-boundary
  quality rather than evidence resolution. Improving it requires a parser v3
  and a newly frozen unseen holdout, because the current holdout's failures
  have already been observed.
- **Holdout labelling was not blind.** The review packet renders the
  machine-proposed change type before the reviewer decides, so the anchoring
  pressure is toward agreement with the detector — these metrics are more
  likely too generous than too harsh.
- **Holdout selection is auditable in outcome but not fully replayable in
  process.** The filing bodies are pinned by SHA-256 and the selection *rules*
  are pinned by `selection_protocol_hash`, so which filings were used is
  provable. The SEC metadata responses that led to choosing them are not
  archived, and a later metadata snapshot is **not** claimed to reproduce the
  original selection byte-for-byte.
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
