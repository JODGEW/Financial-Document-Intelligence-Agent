# Annotation protocol — `real-filing-annotation.v1`

Instructions for a human verifying Item 1A change labels on the controlled
real-filing benchmark corpus.

This document is committed. The packets and annotation files it describes are
**local and gitignored**: they contain filing excerpts and never enter the
repository.

---

## The rule everything else exists to support

**A machine-proposed label is not ground truth.** It is a starting point that
saves you typing. Until you have read a unit yourself and decided, the label is
the detector's opinion about its own output, and scoring the detector against
its own opinion measures nothing.

Only an annotation file whose `annotation_status` is `human_verified` enters a
gold metric. Nothing in this repository can set that status — no script, no
flag, no model. You set it by editing the file.

---

## Statuses

| `annotation_status` | Who sets it | Annotator identity | Counts as gold |
|---|---|---|---|
| `unreviewed` | tooling | must be absent | no |
| `machine_proposed` | packet generator | **must be absent** | no |
| `human_in_progress` | you | required | no |
| `human_verified` | you | required, with timestamp | **yes** |
| `rejected` | you | required, with timestamp | no |

The schema enforces this: a machine status carrying an `annotator_id` is a
validation error, and `human_verified` without both an `annotator_id` and a
`verification_timestamp` is a validation error. That is deliberate. It means a
tool cannot manufacture a verified label even by accident.

`rejected` is a legitimate outcome. Use it when the pair cannot be labelled
honestly — the extraction is wrong, the section is unusable, the units do not
correspond to anything a reader would call a risk factor. A rejected pair is
counted in the corpus-quality report and excluded from gold metrics. It is not
a failure of the annotation process; hiding it would be.

### Annotator identity is self-asserted

`annotator_id` is bounded local metadata — a name, an email, an initials
string. There is **no authentication for benchmark annotators**, and inventing
one would imply an assurance that does not exist. Reports label it
`self_asserted_local_metadata`.

---

## Labels

One label binds one comparison unit (or unit pair) to an expected outcome.

| Field | Meaning |
|---|---|
| `label_id` | Deterministic, derived from the pair and unit ids. Do not edit. |
| `expected_change_type` | `added` / `removed` / `modified` / `unchanged` / `undetermined` |
| `previous_unit_id` | Unit in the previous filing, or `null` |
| `current_unit_id` | Unit in the current filing, or `null` |
| `expected_reason_code` | Only for `undetermined`; `null` otherwise |
| `expected_evidence_side` | `previous` / `current` / `both` / `none` |
| `expected_direction` | `increased` / `decreased` / `unchanged`, or `null` |
| `reviewer_note` | Free text, ≤500 chars. Never enters a metric or a report. |
| `confidence` | `high` / `medium` / `low` |

Shape rules the validator enforces:

- `added` — current unit only, previous `null`
- `removed` — previous unit only, current `null`
- `modified` / `unchanged` — both units present
- `undetermined` — any shape; may carry a reason code

### `unchanged` asserts absence

`comparison.v1` has no `unchanged` change type: an aligned unit whose content
is identical emits no change at all. So an `unchanged` label is a claim that
**nothing should have been emitted** for that unit. If the detector emits
something, that is a false positive, and the `unchanged_false_positive_rate`
counts it.

### `expected_direction` is optional, and usually should be omitted

Set it only when the unit makes a claim whose direction a reader can verify
from the text — a figure that rose or fell, a count that grew. Leave it `null`
otherwise. `comparison.v1` carries no direction field, so this metric scores the
detector's `direction_consistency` **validator** against your claim; a
speculative direction adds noise rather than signal.

Its denominator is deliberately small. A null value with a zero denominator is
a correct and honest report.

---

## Doing a pair

1. Open `packets/<pair_id>/packet.md` in the local corpus directory.
2. Confirm the section hashes in the packet match the annotation file. If they
   do not, the corpus was rebuilt from different text and the packet is stale.
3. For each aligned unit, read both excerpts and decide the change type
   yourself, **before** looking at `machine_proposed_change_type`.
4. Where an excerpt is not enough, read the full extracted section at
   `build/<pair_id>/<side>_item_1a.txt`.
5. Edit `annotations/<pair_id>.machine_proposed.json`, correct the labels, and
   save it as `annotations/<pair_id>.json`.
6. Set `confidence` honestly. `low` is a real answer and is reported in the
   confidence distribution.
7. Only then set `annotation_status` to `human_verified` and fill in
   `annotator_id` and `verification_timestamp` (ISO-8601 with a UTC offset).
8. Validate:

   ```bash
   python scripts/create_real_filing_annotation_packets.py \
       --validate benchmark_data/real_filings_v1/annotations/<pair_id>.json
   ```

Do not skim a machine proposal and mark it verified. A rubber-stamped label is
indistinguishable in the data from a considered one, and it silently converts
"the detector agrees with itself" into a published accuracy number.

---

## Labels bind to exact text

Every annotation carries `previous_section_hash` and `current_section_hash`.
If the corpus is rebuilt and either section's text changes, the evaluator
refuses with `annotation_section_hash_drift` rather than reusing your labels.

The labels are not merely stale in that case — they are about different
content. Re-verify against the new build.

Likewise, a `previous_unit_id` or `current_unit_id` that does not exist in the
build is rejected (`annotation_unknown_unit_reference`), as is a
previous-side id used on the current side.

---

## Inter-annotator agreement

**Not implemented in v1.** Every pair has one annotator, so no agreement
statistic exists and none is reported. A single-annotator benchmark cannot
distinguish "the detector is wrong" from "the annotator read it differently",
and that limitation is stated in `BENCHMARK.md` rather than papered over with a
number.
