# What a Holdout Evaluation Actually Found

I built a document comparison agent that reads two SEC 10-K filings and reports how the risk factors changed. To find out whether it worked, I froze a holdout corpus of 10 issuer pairs (20 filings) selected from SEC metadata only, ran the frozen parser over it once, and had a human label the results.

The interesting part was not the accuracy number. It was that across this project I hit the same defect five times, in five different subsystems. Every instance is a variant of one mistake: **an assertion derived from the wrong source**. Something in the system claimed a fact it was not entitled to claim, because the value it read was not the value that carried the meaning.

Four of the five surfaced during the holdout evaluation described here. The first predates it: I found and fixed the review-queue threshold earlier, while building the chat governance layer, and it is included because it is the cleanest example of the shape.

I am listing them in order of increasing consequence.

All file and line references resolve at commit `8e3e146` unless otherwise noted. The one exception is the citation in section 5, which is pinned to `6e3e405` because that commit holds the pre-fix code the section is about.

## 1. A control that arithmetic made unreachable

The chat governance layer holds an answer for human review when its weighted risk score crosses a threshold. That threshold is `require_review_at_or_above: 0.75` (`policies/risk_thresholds.yaml`). The score is a weighted sum of three signals with weights 0.5 (grounding), 0.3 (guardrail), and 0.2 (external context).

Without a guardrail signal, the maximum reachable score is 0.5 + 0.2 = 0.70. The threshold is 0.75. A badly grounded answer that used external context, the exact case the control exists for, scored 0.70 and returned to the user.

The control was real. The queue worked. The threshold was derived from a number nobody had checked against the range of the thing it gated. The repo now documents this at `governance/risk_scorer.py:22-23` and adds a separate grounding floor (`require_review_below_grounding: 0.50`) that does not depend on the weighted sum.

## 2. Correct policy on a path that never runs

The comparison governance module holds a result for review when any validation check fails. The rule is `hold_on_any_failed_check` and it is enabled: `comparison_governance.py:68` (default) and `policies/comparison_risk_policy.yaml:60`. It is enforced at `comparison_governance.py:279`.

The benchmark path never calls it. `scripts/build_real_filing_benchmark.py` and `real_filing_holdout_extraction.py` run ingestion, extraction, and detection, then persist a build record. Neither imports `comparison_governance` for execution. The only references in `real_filing_holdout_extraction.py` (lines 62, 75, 76) are entries in the frozen-file hash list, not calls.

I checked the databases rather than the code path alone. Across the 9 workspace SQLite databases in the holdout corpus, `comparisons` holds 9 rows and `comparison_governance_evaluations` and `comparison_review_items` each hold 0.

So the benchmark measured a detector running without the governance layer that is supposed to gate it. Reading the policy file would have told me governance was on. It was on. It was not in this path.

## 3. A validator defending an invariant the consumer discards

This one is my favorite, because the two halves are both deliberate.

The holdout annotation validator enforces label closure by **unit id**, and says why in its own docstring (`scripts/validate_holdout_human_annotations.py:32-35`): a filing that repeats a heading produces two distinct units, each needs its own label, "so key-level collapse cannot silently drop a duplicate-heading unit from the gold corpus". Someone thought about duplicate headings and wrote a guard.

The evaluator then keys labels by **unit_key** in a plain dict (`scripts/eval_real_filing_benchmark.py:857-860`). Later entries overwrite earlier ones.

This is not hypothetical. Pair `sic-5000s-01` carries 9 human-verified labels that collapse to 7 distinct keys. The filing repeats two category headings, so `business-risks` and `general-risks` each appear twice. In both cases one unit was labeled `unchanged` and the other `modified`:

```
business-risks  unchanged  previous:001  |  business-risks  modified  previous:002
general-risks   unchanged  previous:007  |  general-risks   modified  previous:008
```

The scorer holds sets, so the same key lands in the unchanged set and the changed set at once. In the committed report, `business-risks` and `general-risks` appear in that pair's `unchanged_false_positives` **and** in its `missed` list simultaneously. Two of the four suite-level unchanged false positives come from this collision.

The validator protected the invariant at the door. The consumer dropped it two steps later. Two of 28 labels never reach scoring.

The scoring effect is not small. The detector reported both duplicated keys as `undetermined`, which is the honest output for a unit it could not align. Had the reviewer labeled all four duplicate units `undetermined` too, that pair would have scored 6/6 on recall and 6/6 on change-type accuracy instead of 4/6 and 4/6. So the pair where the detector demonstrably failed to align its own units is also the pair whose score is most sensitive to how a reviewer wrote the labels.

## 4. Two schemas that refuse to read each other

The development corpus and the holdout corpus have different manifest schemas. The holdout stratifies issuers by SIC code and pins the parser hash. The development manifest carries an issuer slate. Neither validator accepts the other's keys.

I mention this one because it is the counterexample. When I pointed the evaluator at the holdout manifest, it exited 2 with `manifest_unknown_keys` and computed nothing. An unknown schema still does: `manifest_schema_version_unsupported`, exit 2. Dispatch now selects a branch on `schema_version` alone and each branch refuses the other (`scripts/eval_real_filing_benchmark.py:214-234`).

This defect cost me an afternoon and produced no wrong number. That is what the other four should have done.

## 5. A function that published the opposite of the truth

Every benchmark report carries a provenance block saying what kind of corpus produced it. A development corpus was inspected while the parser was written, so results over it are in-sample. A holdout was frozen before anyone looked, so results over it are out-of-sample.

The block was built by one function, and at `real_filing_benchmark.py:274`, as of commit `6e3e405`, it read:

```python
"extraction_holdout_evaluation": not development,
```

The field means "a holdout evaluation was performed". The expression means "this corpus is not the development corpus". Those are different claims. Under that line, **naming** a corpus a holdout was sufficient to publish the assertion that a holdout evaluation had been carried out on it.

The first four defects cost me measurements. This one would have produced an artifact stating something false about its own evidentiary status, in a machine-readable field, in a committed file, on a repository presented as a portfolio. A reader scanning the JSON would have found `extraction_holdout_evaluation: true` on a corpus nobody had evaluated.

The fix was to stop deriving it. `corpus_role_fields` now takes the corpus identity and the run lifecycle as separate required arguments with no defaults. Identity is read from the manifest. Lifecycle is passed by the run that knows what it did.

## What the numbers are

All figures below come from `benchmarks/real_filing_holdout_v1/gold_evaluation_report.json`, produced by `item1a_detector.v2` and `comparison_workflow.v2`.

Of 10 frozen pairs, 9 were scored. One (`sic-6000s-01`) was excluded: extraction returned `ambiguous` on both sides, so no review packet and no human label exist for it. It is reported as an exclusion rather than dropped from the denominator.

The eight gold metrics as committed:

| metric | value |
| --- | --- |
| change_precision | 11/24 (0.4583) |
| change_recall | 11/21 (0.5238) |
| change_type_accuracy | 11/21 (0.5238) |
| unchanged_false_positive_rate | 4/7 (0.5714) |
| evidence_resolution_rate | 1570/1570 (1.0) |
| pair_exact_match_rate | 1/9 (0.1111) |
| undetermined_reason_accuracy | 0/0 (null) |
| direction_consistency_accuracy | 0/0 (null) |

The two nulls have zero denominators. They assert nothing. No label in this corpus carried an expected reason code or an expected direction, so those metrics were never exercised.

`generalization_claim_supported` is `false`. It is gated on a human sign-off recorded in the evaluation config, and none exists. It is not derived from coverage, corpus role, or the metric values.

## What the numbers are not

**They are not a risk-factor-level accuracy.** The classifier never operated at that level. Unit boundaries came from the system under test, using a heading regex at `comparison_detector.py:175`:

```python
_HEADING_RE = re.compile(r"^[A-Z][A-Za-z0-9 ,&()'’-]{2,79}Risks?$")
```

The line must end in "Risk" or "Risks".

I counted the headings twice: once over the extracted plain text, and once over the source HTML. The two passes disagreed by one. Only the HTML separates a level-1 category heading from a level-2 individual risk factor that happens to begin with the word "Risks", because the extracted text has all styling stripped and both are then just short standalone lines. The HTML count is the one reported here.

Across the 9 scored pairs there are 36 distinct category headings per side. The regex matched 17 of them. The other 19 failed, and all 19 fail for exactly three narrow reasons:

- 16 headings prefix rather than suffix: "Risks Related to Our Business", "Risks Relating to Regulations". Five of the nine issuers use this form for at least one heading, and three use it for every heading they have.
- 2 are "General Risk Factors", which ends in "Factors".
- 1 is "Operational and Compliance/Legal Risks", which fails only because `/` is not in the character class. Remove the slash and it matches.

The consequence: 7 of the 9 pairs are mis-segmented, and 3 of them (`sic-3000s-01`, `sic-4000s-01`, `sic-4000s-02`) produced a single preamble unit spanning the entire Item 1A section. Each side produced 28 units: 9 preambles (one per pair) plus 19 matched heading occurrences. That second 19 is a different quantity from the 19 misses above, and the two coincide by accident: it counts the 17 distinct matched headings with two of them rendered twice in the same filing. There are 28 human-verified labels, one per unit.

So 11/24 is precision over units the system invented. On three pairs, "the unit" is the whole section.

**The parser is not fixed.** The regex is unchanged, on purpose. I observed these failures on the holdout. Changing the parser in response would convert the holdout into development data and make any subsequent number post-hoc. Fixing it requires a new parser version and a newly frozen holdout.

## Limitations

**The labeling was not blind.** The review packet renders the machine-proposed change type before the human decides (`scripts/create_real_filing_annotation_packets.py:354`), and pre-fills it into the field the reviewer edits (line 223). The anchoring pressure is toward agreement with the detector, which means these metrics are more likely too generous than too harsh.

**Coverage is 9 of 10 pairs.** The excluded pair is not a random omission. It is the one the parser handled worst.

**Level-2 headings were never matched on any pair.** Individual risk factor headings did not become units even on the two pairs that segmented correctly at category level. Every unit is a preamble or a category. Avnet is the clearest case: its previous-side filing marks 56 individual risk factors in bold italic ("Logistics disruptions", "Inventory value decline"), and that side produced 4 units, none of which is one of them.

**Selection is auditable in outcome but not fully replayable in process.** Filing bodies are pinned by SHA-256. The SEC metadata responses that led to selecting those filings are not: `selection_protocol_hash` binds the selection rules, and `metadata_snapshot` records only two timestamps. I can prove which filings I used. I cannot reproduce the decision that chose them.

**Single annotator.** All 9 gold files carry the same annotator id. There is no second reader and no agreement statistic.
