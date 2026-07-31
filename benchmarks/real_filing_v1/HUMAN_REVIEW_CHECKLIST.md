# Human-review checklist — `real_filing_v1`

Companion to [`annotation_protocol.md`](annotation_protocol.md), which defines
the statuses, label fields, and shape rules. This file says **what to do next**,
per pair, given the corpus as actually built.

---

## Current state: every pair is blocked, and none is reviewable yet

| Pair | Issuer | Packet to open | Review readiness | Blocking reason |
|---|---|---|---|---|
| communication-services-01 | Verizon Communications Inc. | — none generated — | blocked | Item 1A `missing` both sides |
| consumer-discretionary-01 | The Home Depot, Inc. | — none generated — | blocked | Item 1A `missing` both sides |
| consumer-staples-01 | The Procter & Gamble Company | — none generated — | blocked | Item 1A `missing` both sides |
| energy-01 | Exxon Mobil Corporation | — none generated — | blocked | Item 1A `missing` both sides |
| financials-01 | JPMorgan Chase & Co. | — none generated — | blocked | Item 1A `missing` both sides |
| health-care-01 | Johnson & Johnson | — none generated — | blocked | Item 1A `missing` both sides |
| industrials-01 | Caterpillar Inc. | — none generated — | blocked | Item 1A `missing` both sides |
| information-technology-01 | Apple Inc. | — none generated — | blocked | Item 1A `missing` both sides |
| information-technology-02 | Microsoft Corporation | — none generated — | blocked | Item 1A `missing` both sides |
| utilities-01 | NextEra Energy, Inc. | — none generated — | blocked | Item 1A `missing` both sides |

**There is no annotation work to do right now.** All 20 filings are
source-verified and all 10 pairs built, but zero risk units were extracted, so
there is nothing for a reviewer to compare. Machine proposals do not exist
either — the packet generator refused all ten with `pair_not_annotatable`
rather than emitting an empty packet.

**Zero labels are `human_verified`. No accuracy claim is supported.**

### Why every pair is blocked

Real EDGAR 10-K HTML contains no `<h1>`–`<h6>` elements. Item 1A headings are
styled `<div>`/`<span>` runs. The existing HTML section path derives
`section_key` from splitter-produced headings only, so no chunk carries the
canonical Item 1A key and extraction correctly records `missing`.

This is the pre-registered failure mode in `BENCHMARK.md` → *Known coverage
limitations*. It was recorded, not worked around.

### The one decision that unblocks review

Extending the section path to recognise Item 1A in heading-less EDGAR HTML is a
**product change**, not a benchmark adjustment. If it is made:

- it must be justified on its own merits and reviewed independently;
- it must **never** be tuned against detector output on these pairs;
- the corpus must be rebuilt afterwards, which changes section hashes and
  therefore invalidates any annotation written against the current build;
- the pairs stay exactly as they are. A pair is never replaced after its
  outcome is known.

Until then, the honest state of this benchmark is: the process runs end to end,
and the workflow cannot yet be measured on real filings.

---

## Per-pair checklist, for when a pair does become reviewable

Work one pair at a time. Steps 3–7 are the protocol's; they are listed here so
the sequence is in one place.

**1. Which packet to open**

```
benchmark_data/real_filings_v1/packets/<pair_id>/packet.md
```

Confirm `previous_section_hash` and `current_section_hash` in the packet match
the annotation file. If they differ, the packet is stale — rebuild, regenerate,
and start over. Do not reconcile by hand.

**2. Which risk units to compare**

The packet lists every alignment as a `(previous_unit_id, current_unit_id)`
row. Compare exactly those, in the order given. Unit ids are deterministic and
derived from the build — treat them as read-only keys, not as labels.

**3. Verifying each change type** — decide yourself *before* reading
`machine_proposed_change_type`:

| Type | You must be able to say |
|---|---|
| `added` | this risk factor exists only in the current filing (`previous_unit_id: null`) |
| `removed` | it exists only in the previous filing (`current_unit_id: null`) |
| `modified` | both sides present, and the substance of the text differs |
| `unchanged` | both sides present and materially identical — a claim that **nothing should be emitted** |
| `undetermined` | you cannot decide from the extracted text alone; record a reason code |

**4. Verifying the evidence side** — set `expected_evidence_side` to the side(s)
a reader must actually read to confirm your call: `previous`, `current`, `both`,
or `none`.

**5. Verifying reason and direction**

- `expected_reason_code`: only for `undetermined`, `null` otherwise.
- `expected_direction`: leave `null` unless the text states a verifiable
  direction (a figure that rose or fell). This scores the detector's
  `direction_consistency` validator, not an emitted direction; a speculative
  value adds noise.

**6. Recording confidence** — `high` / `medium` / `low`, honestly. `low` is a
real answer and appears in the reported confidence distribution.

**7. Annotator identity and timestamp** — set `annotation_status` to
`human_verified` only after you have read the units, then add your own
`annotator_id` and an ISO-8601 `verification_timestamp` with a UTC offset. No
tool in this repository sets these, and none should be asked to. Identity is
self-asserted local metadata; there is no annotator authentication.

**8. Validating the completed file**

```bash
python scripts/create_real_filing_annotation_packets.py \
    --validate benchmark_data/real_filings_v1/annotations/<pair_id>.json
```

Then confirm the gold evaluator accepts it:

```bash
python scripts/eval_real_filing_benchmark.py --json --report run.json
```

**9. Not changing section hashes or unit ids** — never edit `label_id`,
`previous_unit_id`, `current_unit_id`, or either section hash. They bind your
labels to exact text. If the corpus is rebuilt and the text moves, the evaluator
refuses with `annotation_section_hash_drift` — which is the desired behaviour,
because the labels then describe different content.

**10. Rejecting a pair is a legitimate outcome** — if the extraction is wrong or
the units are not recognisably risk factors, set `rejected` with your identity
and a note. It is counted in corpus quality and excluded from gold metrics.
Hiding it would be the failure.

---

## Reminders that outrank convenience

- A machine proposal is the detector's opinion about its own output. Scoring
  the detector against it measures nothing.
- Never rubber-stamp. A skimmed label is indistinguishable in the data from a
  considered one.
- One annotator per pair in v1, so no inter-annotator agreement statistic
  exists and none is reported.
