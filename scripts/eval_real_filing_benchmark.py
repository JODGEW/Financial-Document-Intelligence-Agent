"""Evaluate the Item 1A workflow against the controlled real-filing benchmark.

    python scripts/eval_real_filing_benchmark.py                 # gold evaluation
    python scripts/eval_real_filing_benchmark.py --unlabeled     # execution report
    python scripts/eval_real_filing_benchmark.py --json --report out.json

Two modes, and the difference between them is the whole point.

``--unlabeled`` reports what the workflow DID on real filings: corpus build
outcomes, execution outcomes, output counts, undetermined counts, runtime,
failure codes, and evidence-resolution mechanics. It contains no accuracy
metric, says so in its own payload, and cannot support a quality claim.

The default mode computes gold accuracy metrics — and refuses to compute them
unless every requested pair is backed by a HUMAN-VERIFIED annotation whose
section hashes still match the built corpus, whose source checksums still match
the frozen manifest, whose unit references all exist, and whose detector and
workflow versions match the declared evaluation config. A machine-proposed
label never enters a numerator or a denominator, in any mode, for any reason.

Metric definitions
------------------
Six metrics reuse the synthetic suite's definitions verbatim (change precision,
change recall, change-type accuracy, unchanged false-positive rate,
evidence-resolution rate, undetermined-reason accuracy) so a number here is
comparable to a number there. Three are new and their denominators are
documented in ``METRIC_NOTES`` because real filings differ from fixtures:
direction-consistency (comparison.v1 emits no direction field, so the metric
scores the detector's direction VALIDATOR against a human's directional claim),
pair-level exact match, and every corpus-quality counter.

No pass/fail threshold exists here and none may be added until an initial
human-verified baseline exists and has been explicitly approved. The synthetic
comparison-regression suite remains the merge-blocking deterministic gate, and
this command never touches it, its baseline, or its hashes.

Exit codes: 0 report produced, 1 evaluation refused or a pair failed,
2 invalid configuration or arguments.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import comparison_detector  # noqa: E402
import comparison_reliability  # noqa: E402
import comparison_store  # noqa: E402
import config  # noqa: E402
import real_filing_benchmark as rfb  # noqa: E402
import real_filing_holdout as rfh  # noqa: E402
from scripts import build_real_filing_benchmark as builder  # noqa: E402

EVALUATION_REPORT_VERSION = "real-filing-benchmark.report.v1"

#: The two manifest schemas this evaluator can read. They are validated by
#: their own modules and are never cross-read: see ``load_manifest_dispatch``.
DEV_MANIFEST_SCHEMA = rfb.MANIFEST_SCHEMA_VERSION
HOLDOUT_MANIFEST_SCHEMA = rfh.HOLDOUT_MANIFEST_SCHEMA_VERSION

DEFAULT_EVALUATION_CONFIG = (
    _REPO_ROOT / "benchmarks" / "real_filing_v1" / "evaluation_config.json"
)
DEFAULT_HOLDOUT_EVALUATION_CONFIG = (
    _REPO_ROOT / "benchmarks" / "real_filing_holdout_v1" / "evaluation_config.json"
)

# Prominent, machine-readable honesty markers. Tests assert these strings, so
# they cannot be quietly softened.
UNLABELED_WARNINGS = (
    "NO GOLD ACCURACY METRICS ARE AVAILABLE IN THIS REPORT.",
    "Machine-proposed annotations are NOT verified labels and are not used here.",
    "This report describes execution mechanics only and CANNOT SUPPORT ANY "
    "QUALITY OR ACCURACY CLAIM about the workflow on real filings.",
)

GOLD_SCOPE_NOTE = (
    "Gold metrics cover ONLY pairs whose annotation file is human_verified. "
    "The corpus is controlled and intentionally small; it is not a "
    "statistically representative sample of SEC filings, issuers, or filing "
    "formats."
)

METRIC_NOTES = {
    "change_precision": (
        "Same definition as the synthetic regression suite: matched (unit_key, "
        "change_type) pairs over every change the detector emitted. A detected "
        "change whose unit key cannot be recovered counts in the denominator."
    ),
    "change_recall": (
        "Same definition as the synthetic suite. Denominator is every "
        "human-verified label whose expected_change_type is not 'unchanged'."
    ),
    "change_type_accuracy": (
        "Same definition as the synthetic suite. Denominator is the labels "
        "whose unit was detected at all — a missed unit is a recall failure, "
        "not a type error, and is not double-counted here."
    ),
    "unchanged_false_positive_rate": (
        "Same definition as the synthetic suite. comparison.v1 has no "
        "'unchanged' change type, so an 'unchanged' label asserts ABSENCE: the "
        "numerator counts unchanged-labelled units that appear in the detected "
        "change set."
    ),
    "evidence_resolution_rate": (
        "Same definition as the synthetic suite: evidence references that "
        "resolve to an indexed chunk of the correct filing, over all evidence "
        "references. Computed over human-verified pairs only."
    ),
    "undetermined_reason_accuracy": (
        "Same definition as the synthetic suite. DENOMINATOR DIFFERS ON REAL "
        "FILINGS: it counts human labels that carry an expected_reason_code AND "
        "whose unit the detector actually reported as undetermined. A label "
        "whose unit was not reported undetermined is a change-type error, "
        "already counted by change_type_accuracy."
    ),
    "direction_consistency_accuracy": (
        "NEW METRIC, DEFINITION DIFFERS FROM THE SYNTHETIC SUITE. comparison.v1 "
        "carries no direction field, so the detector never states a direction. "
        "This metric scores the detector's direction_consistency VALIDATOR "
        "against a human's directional claim: for a matched change whose label "
        "sets expected_direction, the check is correct iff that validator "
        "passed. Denominator is matched changes with a labelled direction only, "
        "so it is typically far smaller than the change denominators."
    ),
    "pair_exact_match_rate": (
        "NEW METRIC. A pair matches exactly iff the detected (unit_key, "
        "change_type) set equals the human-verified non-unchanged label set AND "
        "no unchanged-labelled unit was emitted as a change. Denominator is "
        "human-verified pairs."
    ),
}


class EvaluationRefused(rfb.BenchmarkError):
    """Gold evaluation cannot proceed; the report states why."""


# --- Manifest dispatch --------------------------------------------------------
#
# Two corpora exist and they do not share a manifest schema. The development
# corpus (real_filing_v1) uses ``real-filing-benchmark.manifest.v1``; the
# extraction holdout (real_filing_holdout_v1) uses
# ``real-filing-holdout.manifest.v1``, which strata issuers by SIC, pins the
# frozen parser hash, and carries no issuer slate.
#
# The two are NOT merged. Each schema keeps its own required-key set and its
# own validator in its own module, and this evaluator picks a branch on
# ``schema_version`` alone. Teaching either validator to tolerate the other's
# keys would delete exactly the protection that makes a frozen manifest worth
# freezing, so a branch that is handed the other schema refuses.


@dataclass(frozen=True)
class EvaluationManifest:
    """A validated manifest plus everything the evaluator derives from it.

    ``pairs`` is the evaluator's own pair shape, projected from whichever
    schema was read. ``corpus_role`` is immutable corpus identity read from
    the manifest, never a per-run choice.
    """

    schema_version: str
    document: dict[str, Any]
    pairs: list[dict[str, Any]]
    corpus_role: str
    default_config_path: Path
    default_corpus_dir: str
    path: Path


def _resolve_manifest_path(path: str | Path | None) -> Path:
    """Default to the development manifest: the dev path is unchanged."""
    return Path(path) if path is not None else rfb.default_manifest_path()


def peek_schema_version(path: str | Path) -> str:
    """Read ``schema_version`` alone, before any schema is assumed.

    Dispatch cannot itself validate, because validating requires already
    knowing which schema applies. So this reads the one field that decides,
    and refuses anything that does not carry a usable one.
    """
    target = Path(path)
    if not target.exists():
        raise EvaluationRefused(
            "manifest_not_found", f"benchmark manifest not found: {target.name}"
        )
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvaluationRefused("manifest_not_json", f"{target.name}: {exc}") from exc
    if not isinstance(document, dict):
        raise EvaluationRefused(
            "manifest_not_a_mapping", f"{target.name}: expected a JSON object"
        )
    version = document.get("schema_version")
    if not isinstance(version, str) or not version:
        raise EvaluationRefused(
            "manifest_schema_version_missing",
            f"{target.name}: no schema_version, so no validator can be chosen",
        )
    return version


def _require_branch_schema(path: str | Path, expected: str) -> None:
    found = peek_schema_version(path)
    if found != expected:
        raise EvaluationRefused(
            "manifest_schema_dispatch_mismatch",
            f"{Path(path).name}: this branch reads {expected!r} manifests only, "
            f"got {found!r}. Each manifest schema is validated by its own "
            "module; neither is ever read by the other's validator.",
        )


def load_development_manifest(path: str | Path) -> dict[str, Any]:
    """Development-corpus branch. Refuses any other schema."""
    _require_branch_schema(path, DEV_MANIFEST_SCHEMA)
    return rfb.load_manifest(path)


def load_holdout_manifest(path: str | Path) -> dict[str, Any]:
    """Extraction-holdout branch. Refuses any other schema."""
    _require_branch_schema(path, HOLDOUT_MANIFEST_SCHEMA)
    return rfh.load_holdout_manifest(path)


def development_evaluation_pairs(
    document: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """The development manifest already carries the evaluator's pair shape."""
    return [dict(pair) for pair in rfb.manifest_pairs(document)]


def holdout_evaluation_pairs(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project holdout pairs onto the evaluator's pair shape.

    The holdout schema identifies an issuer's sector by the SIC stratum it was
    sampled from (``stratum_label``); the development schema carries a
    free-text ``sector_label``. They are the same reporting field, so the
    projection maps one onto the other here — in the evaluator, at read time —
    rather than either frozen schema growing the other's key.

    Copies are returned: a manifest is a frozen input and is never mutated to
    suit a reader.
    """
    projected = []
    for pair in rfb.manifest_pairs(document):
        projected.append(
            {
                "pair_id": pair["pair_id"],
                "issuer_name": pair["issuer_name"],
                "sector_label": pair["stratum_label"],
                "previous": dict(pair["previous"]),
                "current": dict(pair["current"]),
            }
        )
    return projected


def _holdout_corpus_role(document: Mapping[str, Any]) -> str:
    """Immutable corpus identity, read from the holdout manifest.

    The manifest is the authority on what this corpus IS. The evaluator only
    checks that the identity it states is internally consistent, so a manifest
    cannot name one role while asserting the development flag of the other.
    """
    role = document["corpus_role"]
    declared_development = document["extraction_parser_developed_using_this_corpus"]
    if declared_development != (role == rfb.CORPUS_ROLE_EXTRACTION_DEVELOPMENT):
        raise EvaluationRefused(
            "manifest_corpus_identity_inconsistent",
            f"manifest declares corpus_role {role!r} but "
            f"extraction_parser_developed_using_this_corpus="
            f"{declared_development!r}",
        )
    return role


def corpus_role_for_manifest(document: Mapping[str, Any]) -> str:
    """Immutable corpus identity for either schema, dispatched on version."""
    schema_version = document.get("schema_version")
    if schema_version == DEV_MANIFEST_SCHEMA:
        # The development manifest predates the corpus-role block and carries
        # no role field; its role is the module constant it was frozen under.
        return rfb.REAL_FILINGS_V1_CORPUS_ROLE
    if schema_version == HOLDOUT_MANIFEST_SCHEMA:
        return _holdout_corpus_role(document)
    raise EvaluationRefused(
        "manifest_schema_version_unsupported",
        f"no evaluation branch reads manifest schema_version {schema_version!r}",
    )


def load_manifest_dispatch(path: str | Path | None = None) -> EvaluationManifest:
    """Choose a validation branch on schema_version, then read that schema."""
    target = _resolve_manifest_path(path)
    schema_version = peek_schema_version(target)
    if schema_version == DEV_MANIFEST_SCHEMA:
        document = load_development_manifest(target)
        return EvaluationManifest(
            schema_version=schema_version,
            document=document,
            pairs=development_evaluation_pairs(document),
            corpus_role=corpus_role_for_manifest(document),
            default_config_path=DEFAULT_EVALUATION_CONFIG,
            default_corpus_dir=config.REAL_FILING_BENCHMARK_DIR,
            path=target,
        )
    if schema_version == HOLDOUT_MANIFEST_SCHEMA:
        document = load_holdout_manifest(target)
        return EvaluationManifest(
            schema_version=schema_version,
            document=document,
            pairs=holdout_evaluation_pairs(document),
            corpus_role=corpus_role_for_manifest(document),
            default_config_path=DEFAULT_HOLDOUT_EVALUATION_CONFIG,
            default_corpus_dir=config.REAL_FILING_HOLDOUT_DIR,
            path=target,
        )
    raise EvaluationRefused(
        "manifest_schema_version_unsupported",
        f"{target.name}: no evaluation branch reads manifest schema_version "
        f"{schema_version!r}. Known schemas: "
        f"{[DEV_MANIFEST_SCHEMA, HOLDOUT_MANIFEST_SCHEMA]}",
    )


# --- Corpus state -------------------------------------------------------------


def load_evaluation_config(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path or DEFAULT_EVALUATION_CONFIG)
    if not target.exists():
        raise EvaluationRefused(
            "evaluation_config_not_found",
            f"evaluation config not found: {target.name}",
        )
    return json.loads(target.read_text(encoding="utf-8"))


# --- Generalization claim sign-off --------------------------------------------
#
# "This workflow generalizes to unseen filings" is the one claim in this
# benchmark that no computation can establish. Coverage, out-of-sample corpus
# role, and verified labels are all NECESSARY CONTEXT for that judgement and
# none of them is the judgement. Deriving the claim from any of them — as an
# earlier revision derived it from coverage completeness — means a corpus that
# happens to score cleanly publishes a scientific claim nobody made.
#
# So the claim is gated on a human sign-off recorded in the evaluation config,
# carrying who signed and when. No tool in this repository writes that field;
# it is the same contract as ``human_verified`` on an annotation, and a test
# asserts the absence of any writer.

SIGNOFF_FIELD = "generalization_claim_signoff"

_SIGNOFF_REQUIRED = (
    "signer_id",
    "signed_at_utc",
    "manifest_sha256",
    "acknowledged_pairs_scored",
)
_SIGNOFF_OPTIONAL = ("statement",)

#: Values that look like a filled-in field but identify nobody.
_SIGNER_PLACEHOLDERS = frozenset(
    {
        "",
        "-",
        "n/a",
        "na",
        "none",
        "null",
        "tbd",
        "todo",
        "changeme",
        "unknown",
        "anonymous",
        "signer_id",
        "your name here",
    }
)


def validate_signoff_document(config_document: Mapping[str, Any]) -> dict | None:
    """Return the sign-off block, or None when the config records none.

    A MALFORMED sign-off is a hard configuration error rather than a quiet
    "unsigned". Silently degrading a broken sign-off to false would make a
    typo indistinguishable from a deliberate decision not to sign.
    """
    signoff = config_document.get(SIGNOFF_FIELD)
    if signoff is None:
        return None
    if not isinstance(signoff, dict):
        raise EvaluationRefused(
            "generalization_signoff_malformed",
            f"{SIGNOFF_FIELD} must be a JSON object or null",
        )
    rfb._exact_keys(  # noqa: SLF001 - the repo's one key-checking primitive
        signoff,
        required=_SIGNOFF_REQUIRED,
        optional=_SIGNOFF_OPTIONAL,
        where=SIGNOFF_FIELD,
        error=EvaluationRefused,
        code_prefix="generalization_signoff",
    )
    signer = signoff["signer_id"]
    if not isinstance(signer, str) or signer.strip().lower() in _SIGNER_PLACEHOLDERS:
        raise EvaluationRefused(
            "generalization_signoff_unattributed",
            f"{SIGNOFF_FIELD}.signer_id must identify a person; "
            f"{signer!r} does not",
        )
    rfb._require_iso_timestamp(  # noqa: SLF001
        signoff["signed_at_utc"],
        f"{SIGNOFF_FIELD}.signed_at_utc",
        EvaluationRefused,
        "generalization_signoff",
    )
    digest = signoff["manifest_sha256"]
    if not isinstance(digest, str) or not rfb._SHA256_RE.match(digest):  # noqa: SLF001
        raise EvaluationRefused(
            "generalization_signoff_invalid_manifest_hash",
            f"{SIGNOFF_FIELD}.manifest_sha256 must be 64 lowercase hex "
            "characters naming the manifest that was signed",
        )
    acknowledged = signoff["acknowledged_pairs_scored"]
    if not isinstance(acknowledged, int) or isinstance(acknowledged, bool):
        raise EvaluationRefused(
            "generalization_signoff_invalid_coverage",
            f"{SIGNOFF_FIELD}.acknowledged_pairs_scored must be an integer",
        )
    if "statement" in signoff:
        rfb._require_bounded_str(  # noqa: SLF001
            signoff["statement"],
            f"{SIGNOFF_FIELD}.statement",
            max_chars=2000,
            error=EvaluationRefused,
            code_prefix="generalization_signoff",
        )
    return dict(signoff)


def evaluate_generalization_claim(
    config_document: Mapping[str, Any],
    *,
    manifest_sha256: str,
    holdout_evaluation_performed: bool,
    pairs_scored: int,
    coverage_complete: bool,
    pairs_in_manifest: int,
) -> dict[str, Any]:
    """Decide the claim, and state every reason it is not supported.

    Coverage is reported here as context the signer is required to have
    acknowledged, NOT as a condition that can grant the claim on its own.
    """
    signoff = validate_signoff_document(config_document)
    blocked_by: list[str] = []

    if not holdout_evaluation_performed:
        blocked_by.append(
            "this run did not perform a holdout evaluation: a generalization "
            "claim needs an out-of-sample corpus that this run actually scored"
        )
    if signoff is None:
        blocked_by.append(
            f"no human sign-off is recorded in the evaluation config "
            f"({SIGNOFF_FIELD} is null). The claim that this workflow "
            "generalizes is a human judgement and is never derived from "
            "coverage, corpus role, or metric values."
        )
    else:
        if signoff["manifest_sha256"] != manifest_sha256:
            blocked_by.append(
                "the recorded sign-off names a different manifest than this "
                "run evaluated, so it does not authorize a claim about this "
                "corpus state"
            )
        if signoff["acknowledged_pairs_scored"] != pairs_scored:
            blocked_by.append(
                f"the recorded sign-off acknowledges "
                f"{signoff['acknowledged_pairs_scored']} scored pairs but this "
                f"run scored {pairs_scored}; the signer did not see this "
                "coverage"
            )

    supported = not blocked_by
    return {
        "supported": supported,
        "blocked_by": blocked_by,
        # Coverage is a reported fact standing beside the claim, never the
        # thing that grants it.
        "coverage_complete": coverage_complete,
        "pairs_scored": pairs_scored,
        "pairs_in_manifest": pairs_in_manifest,
        "signoff_present": signoff is not None,
        "signoff_signer_id": signoff["signer_id"] if signoff else None,
        "signoff_signed_at_utc": signoff["signed_at_utc"] if signoff else None,
        "signoff_statement": (signoff or {}).get("statement"),
        "policy": (
            "generalization_claim_supported is true only when a human "
            "sign-off in the evaluation config names the exact manifest this "
            "run evaluated and acknowledges the number of pairs it scored. No "
            "tool in this repository writes that sign-off."
        ),
    }


def require_config_matches_manifest(
    config_document: Mapping[str, Any], manifest: Mapping[str, Any]
) -> None:
    """An evaluation config describes ONE corpus, and must name that corpus.

    Without this, pointing ``--evaluation-config`` at the development config
    while evaluating the holdout would silently score the holdout under the
    other corpus's declared versions and gold-status policy.
    """
    declared = config_document.get("benchmark_id")
    actual = manifest["benchmark_id"]
    if declared != actual:
        raise EvaluationRefused(
            "evaluation_config_benchmark_mismatch",
            f"the evaluation config declares benchmark_id {declared!r} but the "
            f"manifest is {actual!r}. A config describes one corpus; using "
            "another corpus's config would score this run under versions and "
            "policy that were never declared for it.",
        )


def _required_pair_field(pair: Mapping[str, Any], field: str) -> Any:
    """Read a pair field the evaluator cannot proceed without.

    A bare ``pair[field]`` here raised an uncaught KeyError that surfaced as a
    traceback rather than as a stated reason — and it fired on exactly the
    case that matters, a manifest schema whose projection is incomplete. This
    is a BenchmarkError so it exits as a configuration failure with a code.
    """
    if field not in pair:
        raise EvaluationRefused(
            "evaluation_pair_missing_field",
            f"manifest pair {pair.get('pair_id', '<unknown>')!r} has no "
            f"{field!r}: the evaluation projection for this manifest schema "
            "did not produce a complete pair",
        )
    return pair[field]


def collect_pair_state(
    pair: dict[str, Any], layout: rfb.CorpusLayout
) -> dict[str, Any]:
    """Everything locally known about one pair, with per-stage failures kept.

    A missing artifact is recorded as a state, never raised: the corpus-quality
    report exists precisely to say how many pairs did not get as far as the
    next stage.
    """
    pair_id = _required_pair_field(pair, "pair_id")
    state: dict[str, Any] = {
        "pair_id": pair_id,
        "issuer_name": _required_pair_field(pair, "issuer_name"),
        "sector_label": _required_pair_field(pair, "sector_label"),
        "build_record": None,
        "detection_result": None,
        "annotation": None,
        "annotation_status": None,
        "problems": [],
    }
    try:
        state["build_record"] = builder.load_build_record(pair_id, layout)
    except rfb.BenchmarkError as exc:
        state["problems"].append({"code": exc.code, "detail": exc.message})
        return state

    state["detection_result"] = builder.load_detection_result(pair_id, layout)

    annotation_path = layout.annotation_path(pair_id)
    if not annotation_path.exists():
        state["problems"].append(
            {
                "code": "annotation_not_found",
                "detail": (
                    "no completed annotation file; a machine-proposed packet is "
                    "not one"
                ),
            }
        )
        return state
    try:
        annotation = rfb.load_annotation(annotation_path)
        rfb.validate_annotation_against_build(annotation, state["build_record"])
    except rfb.BenchmarkError as exc:
        state["problems"].append({"code": exc.code, "detail": exc.message})
        return state
    state["annotation"] = annotation
    state["annotation_status"] = annotation["annotation_status"]
    return state


def verify_source_checksums(
    pair: dict[str, Any], build_record: dict[str, Any]
) -> list[dict[str, str]]:
    """The built corpus must still match the frozen manifest digests."""
    problems = []
    for side, payload in rfb.pair_sides(pair):
        recorded = build_record[side].get("source_sha256")
        if recorded != payload["expected_sha256"]:
            problems.append(
                {
                    "code": "source_checksum_drift",
                    "detail": (
                        f"{side}: the built corpus was produced from different "
                        "bytes than the frozen manifest names"
                    ),
                }
            )
    return problems


# --- Corpus quality -----------------------------------------------------------


def corpus_quality(
    pairs: list[dict[str, Any]], states: list[dict[str, Any]]
) -> dict[str, Any]:
    """Stage-by-stage counts. Every number is a count, not a rate."""
    built = [state for state in states if state["build_record"]]

    def _side_outcomes(outcome: str) -> int:
        return sum(
            1
            for state in built
            for side in ("previous", "current")
            if state["build_record"][side]["extraction_outcome"] == outcome
        )

    pairs_with_both_extracted = sum(
        1 for state in built if rfb.build_is_evaluable(state["build_record"])
    )
    return {
        "pairs_requested": len(pairs),
        "pairs_source_verified": sum(
            1
            for state in built
            if not verify_source_checksums(
                _pair_for(pairs, state["pair_id"]), state["build_record"]
            )
        ),
        "pairs_built": len(built),
        "pairs_extracted": pairs_with_both_extracted,
        "pairs_missing_section": sum(
            1
            for state in built
            if any(
                state["build_record"][side]["extraction_outcome"]
                == rfb.EXTRACTION_MISSING
                for side in ("previous", "current")
            )
        ),
        "pairs_ambiguous_section": sum(
            1
            for state in built
            if any(
                state["build_record"][side]["extraction_outcome"]
                == rfb.EXTRACTION_AMBIGUOUS
                for side in ("previous", "current")
            )
        ),
        "pairs_parse_failed": sum(
            1
            for state in built
            if any(
                state["build_record"][side]["extraction_outcome"]
                == rfb.EXTRACTION_PARSE_FAILED
                for side in ("previous", "current")
            )
        ),
        "pairs_human_verified": sum(
            1
            for state in states
            if state["annotation"] and rfb.is_gold(state["annotation"])
        ),
        "pairs_machine_proposed_only": sum(
            1
            for state in states
            if state["annotation"]
            and state["annotation"]["annotation_status"]
            == rfb.ANNOTATION_MACHINE_PROPOSED
        ),
        "filing_extraction_outcomes": {
            outcome: _side_outcomes(outcome) for outcome in rfb.EXTRACTION_OUTCOMES
        },
    }


def _pair_for(pairs: list[dict[str, Any]], pair_id: str) -> dict[str, Any]:
    return next(pair for pair in pairs if pair["pair_id"] == pair_id)


# --- Scoring scope ------------------------------------------------------------
#
# A pair whose Item 1A could not be extracted on both sides can never carry a
# human-verified annotation: no review packet is generated for it, so there is
# nothing for an annotator to verify. Treating its missing annotation as a
# refusal reason would make one unextractable pair suppress the metrics for
# every other pair in the corpus. Treating it as nothing at all would quietly
# shrink the denominator instead.
#
# So it is neither: the pair is excluded from scoring and reported as excluded,
# by pair id and with the extraction outcomes that blocked it.

#: The only condition under which a pair may be excluded rather than refuse.
EXCLUSION_EXTRACTION_BLOCKED = "extraction_blocked"


def _is_extraction_blocked(state: Mapping[str, Any]) -> bool:
    """True only for a built pair that extraction itself made unscorable.

    Deliberately narrow. The pair must have built, must have failed
    ``build_is_evaluable``, and must carry no problem other than the missing
    annotation that follows from it. Any other defect — drift, a rejected
    annotation, a machine proposal, an unreadable build record — is not an
    extraction block and still refuses the run.
    """
    record = state["build_record"]
    if not record or rfb.build_is_evaluable(record):
        return False
    codes = {problem["code"] for problem in state["problems"]}
    return codes <= {"annotation_not_found"}


def partition_scoring_scope(
    states: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split states into what gold metrics cover and what they cannot."""
    scorable: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for state in states:
        annotation = state["annotation"]
        if annotation is not None and rfb.is_gold(annotation):
            scorable.append(state)
            continue
        if _is_extraction_blocked(state):
            record = state["build_record"]
            excluded.append(
                {
                    "pair_id": state["pair_id"],
                    "code": EXCLUSION_EXTRACTION_BLOCKED,
                    "previous_extraction_outcome": (
                        record["previous"]["extraction_outcome"]
                    ),
                    "current_extraction_outcome": (
                        record["current"]["extraction_outcome"]
                    ),
                    "detail": (
                        "Item 1A was not extracted on both sides, so no review "
                        "packet and no human-verified annotation exist for this "
                        "pair. It is excluded from every gold numerator and "
                        "denominator and is reported here rather than dropped."
                    ),
                }
            )
    return scorable, excluded


def scoring_scope_report(
    pairs: list[dict[str, Any]],
    scorable: list[dict[str, Any]],
    excluded: list[dict[str, Any]],
) -> dict[str, Any]:
    """What the gold metrics below actually cover, stated as counts and ids."""
    return {
        "pairs_in_manifest": len(pairs),
        "pairs_scored": len(scorable),
        "pairs_excluded": len(excluded),
        "scored_pair_ids": sorted(state["pair_id"] for state in scorable),
        "excluded_pairs": sorted(excluded, key=lambda item: item["pair_id"]),
        "coverage_complete": len(excluded) == 0,
        "note": (
            "Gold metric denominators below count the scored pairs only. "
            "Excluded pairs are listed above with the reason they could not be "
            "scored; they are never silently absent from the denominator."
        ),
    }


# --- Gold scoring -------------------------------------------------------------


def _candidate_unit_keys(build_record: dict[str, Any]) -> set[str]:
    return {
        unit["unit_key"]
        for side in ("previous", "current")
        for unit in build_record[side]["units"]
    }


def _unit_key_of_change(change_id: str, candidate_keys: set[str]) -> str | None:
    """Recover a change's unit key by recomputing the deterministic change_id.

    Identical technique to the synthetic regression evaluator, so the two score
    detector output the same way.
    """
    for key in sorted(candidate_keys):
        for change_type in ("added", "removed", "modified", "undetermined"):
            if change_id == comparison_detector._change_id(change_type, key):  # noqa: SLF001
                return key
    return None


def _unit_key_from_unit_id(unit_id: str) -> str:
    """'previous:003:cyber-risks' -> 'cyber-risks'."""
    parts = unit_id.split(":", 2)
    return parts[2] if len(parts) == 3 else unit_id


def label_unit_key(label: dict[str, Any]) -> str:
    """The detector-comparable key for a human label.

    Mirrors how ``comparison_detector`` keys its own changes: a two-sided
    change is keyed by the PREVIOUS unit, a one-sided change by its only unit.
    """
    reference = label["previous_unit_id"] or label["current_unit_id"]
    return _unit_key_from_unit_id(reference)


def score_pair(state: dict[str, Any]) -> dict[str, Any]:
    """Score one human-verified pair against detector output."""
    build_record = state["build_record"]
    annotation = state["annotation"]
    result = state["detection_result"] or {"changes": []}
    candidates = _candidate_unit_keys(build_record)

    detected: set[tuple[str, str]] = set()
    detected_keys: set[str] = set()
    detected_changes: dict[str, dict[str, Any]] = {}
    for change in result.get("changes", []):
        key = _unit_key_of_change(change["change_id"], candidates)
        label_key = key if key is not None else f"unlabeled:{change['change_id']}"
        detected.add((label_key, change["change_type"]))
        detected_keys.add(label_key)
        detected_changes[label_key] = change

    expected: set[tuple[str, str]] = set()
    unchanged_keys: set[str] = set()
    labels_by_key: dict[str, dict[str, Any]] = {}
    for label in annotation["labels"]:
        key = label_unit_key(label)
        labels_by_key[key] = label
        if label["expected_change_type"] == "unchanged":
            unchanged_keys.add(key)
        else:
            expected.add((key, label["expected_change_type"]))

    matched = detected & expected
    type_denominator = [
        (key, change_type)
        for key, change_type in expected
        if key in detected_keys
    ]

    reason_total = 0
    reason_correct = 0
    for key, label in labels_by_key.items():
        code = label.get("expected_reason_code")
        if not code:
            continue
        change = detected_changes.get(key)
        if change is None or change["change_type"] != "undetermined":
            continue
        reason_total += 1
        if (change.get("undetermined_reason") or "").startswith(code):
            reason_correct += 1

    direction_total = 0
    direction_correct = 0
    for key, change_type in matched:
        label = labels_by_key.get(key)
        if not label or not label.get("expected_direction"):
            continue
        direction_total += 1
        change = detected_changes.get(key)
        statuses = [
            check["status"]
            for check in (change or {}).get("validation", [])
            if check.get("check") == "direction_consistency"
        ]
        if statuses and all(status == "passed" for status in statuses):
            direction_correct += 1

    evidence = (build_record.get("execution") or {})
    evidence_total = evidence.get("evidence_total", 0)
    evidence_resolved = (
        evidence_total
        - evidence.get("evidence_unresolved", 0)
        - evidence.get("evidence_foreign", 0)
    )

    exact_match = (
        detected == expected and not (unchanged_keys & detected_keys)
    )

    return {
        "pair_id": state["pair_id"],
        "sector_label": state["sector_label"],
        "annotation_status": annotation["annotation_status"],
        "annotation_hash": rfb.annotation_hash(annotation),
        "build_hash": build_record["build_hash"],
        "label_count": len(annotation["labels"]),
        "expected_change_count": len(expected),
        "detected_change_count": len(detected),
        "matched_count": len(matched),
        "false_positives": sorted(detected - expected),
        "missed": sorted(expected - detected),
        "type_denominator": len(type_denominator),
        "type_correct": len([item for item in type_denominator if item in detected]),
        "unchanged_label_count": len(unchanged_keys),
        "unchanged_false_positives": sorted(unchanged_keys & detected_keys),
        "evidence_total": evidence_total,
        "evidence_resolved": evidence_resolved,
        "undetermined_reason_total": reason_total,
        "undetermined_reason_correct": reason_correct,
        "direction_total": direction_total,
        "direction_correct": direction_correct,
        "exact_match": exact_match,
        "metrics": {
            "change_precision": rfb.rate(
                len(matched), len(detected), "change_precision"
            ),
            "change_recall": rfb.rate(len(matched), len(expected), "change_recall"),
            "change_type_accuracy": rfb.rate(
                len([item for item in type_denominator if item in detected]),
                len(type_denominator),
                "change_type_accuracy",
            ),
            "unchanged_false_positive_rate": rfb.rate(
                len(unchanged_keys & detected_keys),
                len(unchanged_keys),
                "unchanged_false_positive_rate",
            ),
            "evidence_resolution_rate": rfb.rate(
                evidence_resolved, evidence_total, "evidence_resolution_rate"
            ),
            "undetermined_reason_accuracy": rfb.rate(
                reason_correct, reason_total, "undetermined_reason_accuracy"
            ),
            "direction_consistency_accuracy": rfb.rate(
                direction_correct, direction_total, "direction_consistency_accuracy"
            ),
        },
    }


def aggregate_gold_metrics(scores: list[dict[str, Any]]) -> dict[str, Any]:
    """Suite-level gold metrics with every denominator stated explicitly."""
    matched = sum(score["matched_count"] for score in scores)
    detected = sum(score["detected_change_count"] for score in scores)
    expected = sum(score["expected_change_count"] for score in scores)
    return {
        "change_precision": rfb.rate(matched, detected, "change_precision"),
        "change_recall": rfb.rate(matched, expected, "change_recall"),
        "change_type_accuracy": rfb.rate(
            sum(score["type_correct"] for score in scores),
            sum(score["type_denominator"] for score in scores),
            "change_type_accuracy",
        ),
        "unchanged_false_positive_rate": rfb.rate(
            sum(len(score["unchanged_false_positives"]) for score in scores),
            sum(score["unchanged_label_count"] for score in scores),
            "unchanged_false_positive_rate",
        ),
        "evidence_resolution_rate": rfb.rate(
            sum(score["evidence_resolved"] for score in scores),
            sum(score["evidence_total"] for score in scores),
            "evidence_resolution_rate",
        ),
        "undetermined_reason_accuracy": rfb.rate(
            sum(score["undetermined_reason_correct"] for score in scores),
            sum(score["undetermined_reason_total"] for score in scores),
            "undetermined_reason_accuracy",
        ),
        "direction_consistency_accuracy": rfb.rate(
            sum(score["direction_correct"] for score in scores),
            sum(score["direction_total"] for score in scores),
            "direction_consistency_accuracy",
        ),
        "pair_exact_match_rate": rfb.rate(
            sum(1 for score in scores if score["exact_match"]),
            len(scores),
            "pair_exact_match_rate",
        ),
    }


def label_statistics(states: list[dict[str, Any]]) -> dict[str, Any]:
    """Human-review label counts and confidence distribution.

    Reviewer notes are deliberately absent: they are review context for a
    person and never an evaluation output.
    """
    confidence = {level: 0 for level in rfb.CONFIDENCE_LEVELS}
    by_type = {change_type: 0 for change_type in rfb.EXPECTED_CHANGE_TYPES}
    directions = 0
    total = 0
    for state in states:
        annotation = state["annotation"]
        if not annotation or not rfb.is_gold(annotation):
            continue
        for label in annotation["labels"]:
            total += 1
            confidence[label["confidence"]] += 1
            by_type[label["expected_change_type"]] += 1
            if label["expected_direction"]:
                directions += 1
    return {
        "human_verified_label_count": total,
        "labels_by_expected_change_type": by_type,
        "confidence_distribution": confidence,
        "labels_with_expected_direction": directions,
        "reviewer_notes_included": False,
    }


# --- Operational metrics ------------------------------------------------------


def operational_metrics(
    states: list[dict[str, Any]], layout: rfb.CorpusLayout
) -> dict[str, Any]:
    """Runtime and failure reporting, from durable attempt and job records."""
    durations: list[float] = []
    attempts_total = 0
    retries = 0
    lease_reclaims = 0
    job_attempts = 0
    failures: dict[str, int] = {}

    for state in states:
        record = state["build_record"]
        if not record:
            continue
        execution = record.get("execution") or {}
        attempts = execution.get("attempts") or []
        attempts_total += len(attempts)
        # A second or later attempt on the same comparison is a re-execution.
        retries += max(0, len(attempts) - 1)
        for attempt in attempts:
            elapsed = comparison_reliability.elapsed_ms_for(attempt)
            if elapsed is not None:
                durations.append(elapsed / 1000.0)
            if attempt.get("failure_code"):
                code = attempt["failure_code"]
                failures[code] = failures.get(code, 0) + 1
        if execution.get("failure_code"):
            code = execution["failure_code"]
            failures[code] = failures.get(code, 0) + 1

        comparison_id = execution.get("comparison_id")
        db_path = layout.workspace_dir(state["pair_id"]) / "comparisons.db"
        if comparison_id and db_path.exists():
            for job in comparison_store.list_detection_jobs(
                comparison_id, db_path=db_path
            ):
                job_attempts += 1
                for event in comparison_store.list_detection_job_events(
                    job["job_id"], db_path=db_path
                ):
                    if event.get("event_type") == "detection_job_reclaimed":
                        lease_reclaims += 1

    return {
        "percentile_method": comparison_reliability.PERCENTILE_METHOD,
        "detection_duration_sample_size": len(durations),
        "detection_duration_seconds_p50": _rounded(
            comparison_reliability.percentile(durations, 0.50)
        ),
        "detection_duration_seconds_p95": _rounded(
            comparison_reliability.percentile(durations, 0.95)
        ),
        "total_detection_attempts": attempts_total,
        "retries": retries,
        "detection_jobs": job_attempts,
        "lease_reclaims": lease_reclaims,
        "failures_by_code": dict(sorted(failures.items())),
        "execution_mode": (
            "direct synchronous detection: the benchmark build calls the "
            "existing detector directly, so no durable job, lease, or retry is "
            "created. Zero jobs and zero reclaims mean that path was not "
            "exercised, not that it succeeded."
        ),
    }


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


# --- Reports ------------------------------------------------------------------


def _provenance(
    manifest: dict[str, Any],
    manifest_path: str | Path | None,
    states: list[dict[str, Any]],
    config_document: dict[str, Any],
    *,
    holdout_evaluation_performed: bool,
    generalization_claim_supported: bool,
) -> dict[str, Any]:
    gold_annotations = [
        state["annotation"]
        for state in states
        if state["annotation"] and rfb.is_gold(state["annotation"])
    ]
    return {
        "report_version": EVALUATION_REPORT_VERSION,
        "benchmark_id": manifest["benchmark_id"],
        "benchmark_version": manifest["benchmark_version"],
        "manifest_status": manifest["status"],
        "manifest_hash": rfb.manifest_hash(manifest_path),
        "annotation_hash": (
            rfb.annotations_hash(gold_annotations) if gold_annotations else None
        ),
        "detector_version": comparison_detector.DETECTOR_VERSION,
        "workflow_version": comparison_store.WORKFLOW_VERSION,
        "declared_detector_version": config_document["declared_detector_version"],
        "declared_workflow_version": config_document["declared_workflow_version"],
        "metric_definitions_version": rfb.METRIC_DEFINITIONS_VERSION,
        "annotation_protocol_version": rfb.ANNOTATION_PROTOCOL_VERSION,
        "selection_protocol_version": manifest["selection_protocol_version"],
        # Corpus validity is provenance, not a footnote: any number in this
        # report is qualified by whether the corpus was seen during extraction
        # development. Identity (which corpus this is) is read from the
        # manifest; lifecycle (what THIS run did with it) is derived from the
        # run and passed in. Neither is defaulted.
        **rfb.corpus_role_fields(
            corpus_role_for_manifest(manifest),
            holdout_evaluation_performed=holdout_evaluation_performed,
            generalization_claim_supported=generalization_claim_supported,
        ),
        "commit_sha": rfb.repo_commit_sha(_REPO_ROOT),
        "evaluated_at": rfb.utc_now_iso(),
    }


def unlabeled_report(
    manifest: dict[str, Any],
    manifest_path: str | Path | None,
    pairs: list[dict[str, Any]],
    states: list[dict[str, Any]],
    layout: rfb.CorpusLayout,
    config_document: dict[str, Any],
) -> dict[str, Any]:
    """Execution mechanics only. Contains no accuracy metric, and says so."""
    executions = []
    for state in states:
        record = state["build_record"]
        if not record:
            executions.append(
                {
                    "pair_id": state["pair_id"],
                    "built": False,
                    "problems": state["problems"],
                }
            )
            continue
        execution = record.get("execution") or {}
        executions.append(
            {
                "pair_id": state["pair_id"],
                "built": True,
                "previous_extraction_outcome": record["previous"]["extraction_outcome"],
                "current_extraction_outcome": record["current"]["extraction_outcome"],
                "previous_unit_count": record["previous"]["unit_count"],
                "current_unit_count": record["current"]["unit_count"],
                "executed": execution.get("executed", False),
                "lifecycle": execution.get("lifecycle"),
                "failure_code": execution.get("failure_code"),
                "change_count": execution.get("change_count", 0),
                "changes_by_type": execution.get("changes_by_type", {}),
                "undetermined_count": (execution.get("changes_by_type") or {}).get(
                    "undetermined", 0
                ),
                "undetermined_reason_codes": execution.get(
                    "undetermined_reason_codes", {}
                ),
                "evidence_total": execution.get("evidence_total", 0),
                "evidence_unresolved": execution.get("evidence_unresolved", 0),
                "evidence_foreign": execution.get("evidence_foreign", 0),
                "annotation_status": state["annotation_status"],
            }
        )

    return {
        # An unlabeled run reports mechanics and computes no gold metric, so
        # it is not a holdout evaluation even when the corpus is the holdout.
        **_provenance(
            manifest,
            manifest_path,
            states,
            config_document,
            holdout_evaluation_performed=False,
            generalization_claim_supported=False,
        ),
        "mode": "unlabeled_execution",
        "gold_metrics_available": False,
        "gold_metrics": None,
        "warnings": list(UNLABELED_WARNINGS),
        "corpus_quality": corpus_quality(pairs, states),
        "executions": executions,
        "evidence_resolution_mechanics": {
            "evidence_total": sum(
                item.get("evidence_total", 0) for item in executions
            ),
            "evidence_unresolved": sum(
                item.get("evidence_unresolved", 0) for item in executions
            ),
            "evidence_foreign": sum(
                item.get("evidence_foreign", 0) for item in executions
            ),
            "note": (
                "Counts of references that did or did not resolve to an indexed "
                "chunk of the correct filing. This is a mechanical property of "
                "the pipeline, not a measure of whether the change was right."
            ),
        },
        "operational": operational_metrics(states, layout),
    }


def refusal_reasons(
    pairs: list[dict[str, Any]],
    states: list[dict[str, Any]],
    config_document: dict[str, Any],
    *,
    new_run: bool,
    excluded_pair_ids: frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    """Every reason gold evaluation cannot proceed, collected not short-circuited.

    ``excluded_pair_ids`` names pairs already reported as out of scoring scope
    because extraction blocked them. For those, and only those, the missing
    annotation that necessarily follows is not also a refusal reason — it is
    reported in ``scoring_scope``. Every other problem on those pairs, source
    checksum drift included, still refuses.
    """
    reasons: list[dict[str, str]] = []

    if not new_run:
        if config_document["declared_detector_version"] != (
            comparison_detector.DETECTOR_VERSION
        ):
            reasons.append(
                {
                    "code": "detector_version_mismatch",
                    "detail": (
                        f"the evaluation config declares "
                        f"{config_document['declared_detector_version']!r} but "
                        f"the live detector is "
                        f"{comparison_detector.DETECTOR_VERSION!r}. Metrics "
                        "under an undeclared version would not describe the "
                        "workflow the config names; create a new run "
                        "explicitly (--new-run)."
                    ),
                }
            )
        if config_document["declared_workflow_version"] != (
            comparison_store.WORKFLOW_VERSION
        ):
            reasons.append(
                {
                    "code": "workflow_version_mismatch",
                    "detail": (
                        f"the evaluation config declares "
                        f"{config_document['declared_workflow_version']!r} but "
                        f"the live workflow is "
                        f"{comparison_store.WORKFLOW_VERSION!r}"
                    ),
                }
            )

    for state in states:
        pair_id = state["pair_id"]
        excluded = pair_id in excluded_pair_ids
        for problem in state["problems"]:
            if excluded and problem["code"] == "annotation_not_found":
                continue
            reasons.append({"code": problem["code"], "detail": f"{pair_id}: {problem['detail']}"})
        if state["build_record"]:
            for problem in verify_source_checksums(
                _pair_for(pairs, pair_id), state["build_record"]
            ):
                reasons.append(
                    {"code": problem["code"], "detail": f"{pair_id}: {problem['detail']}"}
                )
        annotation = state["annotation"]
        if annotation is None:
            continue
        if not rfb.is_gold(annotation):
            reasons.append(
                {
                    "code": "pair_not_human_verified",
                    "detail": (
                        f"{pair_id}: annotation_status is "
                        f"{annotation['annotation_status']!r}. Only "
                        f"{rfb.GOLD_STATUS!r} labels enter gold metrics — a "
                        "machine proposal is not a verified label."
                    ),
                }
            )
    return reasons


def gold_report(
    manifest: dict[str, Any],
    manifest_path: str | Path | None,
    pairs: list[dict[str, Any]],
    states: list[dict[str, Any]],
    layout: rfb.CorpusLayout,
    config_document: dict[str, Any],
    *,
    new_run: bool = False,
) -> dict[str, Any]:
    """Gold metrics, or an explicit refusal carrying every reason."""
    scorable, excluded = partition_scoring_scope(states)
    scope = scoring_scope_report(pairs, scorable, excluded)
    reasons = refusal_reasons(
        pairs,
        states,
        config_document,
        new_run=new_run,
        excluded_pair_ids=frozenset(item["pair_id"] for item in excluded),
    )
    if not reasons and not scorable:
        # Nothing refused, but nothing is verified either. Reporting empty
        # metrics as a successful gold run would state a result that has no
        # labels behind it.
        reasons = [
            {
                "code": "no_human_verified_pairs",
                "detail": (
                    "no pair in scope carries a human-verified annotation, so "
                    "there is nothing to score"
                ),
            }
        ]
    if reasons:
        return {
            **_provenance(
                manifest,
                manifest_path,
                states,
                config_document,
                # A refused run evaluated nothing.
                holdout_evaluation_performed=False,
                generalization_claim_supported=False,
            ),
            "mode": "gold_evaluation",
            "refused": True,
            "gold_metrics_available": False,
            "gold_metrics": None,
            "refusal_reasons": reasons,
            "scoring_scope": scope,
            "corpus_quality": corpus_quality(pairs, states),
            "note": (
                "Gold evaluation was REFUSED. No accuracy metric is reported, "
                "because reporting one would require treating unverified or "
                "drifted labels as ground truth."
            ),
        }

    # Scored pairs only. `states` also holds pairs extraction blocked out of
    # scope, which carry no annotation to score against.
    scored = [score_pair(state) for state in scorable]

    corpus_role = corpus_role_for_manifest(manifest)
    is_holdout = corpus_role == rfb.CORPUS_ROLE_EXTRACTION_HOLDOUT
    # Lifecycle, derived from what this run actually did: gold metrics were
    # computed, over a corpus whose frozen identity is out-of-sample.
    holdout_evaluation_performed = is_holdout
    # The generalization claim is NOT derived from any of that. It needs a
    # human sign-off naming this exact corpus state; coverage travels with it
    # as reported context rather than as the thing that grants it.
    claim = evaluate_generalization_claim(
        config_document,
        manifest_sha256=rfb.manifest_hash(manifest_path),
        holdout_evaluation_performed=holdout_evaluation_performed,
        pairs_scored=scope["pairs_scored"],
        coverage_complete=scope["coverage_complete"],
        pairs_in_manifest=scope["pairs_in_manifest"],
    )
    generalization_claim_supported = claim["supported"]
    blocked_by = claim["blocked_by"]

    return {
        "generalization_claim": claim,
        **_provenance(
            manifest,
            manifest_path,
            states,
            config_document,
            holdout_evaluation_performed=holdout_evaluation_performed,
            generalization_claim_supported=generalization_claim_supported,
        ),
        "mode": "gold_evaluation",
        "refused": False,
        "gold_metrics_available": True,
        "scope": GOLD_SCOPE_NOTE,
        "scoring_scope": scope,
        "generalization_claim_blocked_by": blocked_by,
        "pass_fail_thresholds": None,
        "threshold_policy": (
            "No pass/fail threshold exists for real-filing metrics and none may "
            "be introduced until an initial human-verified baseline exists and "
            "has been explicitly approved. The synthetic comparison-regression "
            "suite remains the merge-blocking deterministic gate."
        ),
        "metric_notes": METRIC_NOTES,
        "zero_denominator_policy": rfb.ZERO_DENOMINATOR_POLICY,
        "corpus_quality": corpus_quality(pairs, states),
        "gold_metrics": aggregate_gold_metrics(scored),
        "per_pair": scored,
        "label_statistics": label_statistics(states),
        "operational": operational_metrics(states, layout),
    }


# --- CLI ----------------------------------------------------------------------


def _print_rate(name: str, metric: dict[str, Any]) -> None:
    value = "null" if metric["value"] is None else f"{metric['value']:.4f}"
    suffix = "  ZERO-DENOMINATOR (asserts nothing)" if metric["zero_denominator"] else ""
    print(
        f"  {name:<34} {value:>8} "
        f"({metric['numerator']}/{metric['denominator']}){suffix}"
    )


def print_report(report: dict[str, Any]) -> None:
    print("Controlled real-filing benchmark — Item 1A comparison workflow")
    print(
        f"  benchmark={report['benchmark_id']} v{report['benchmark_version']} "
        f"status={report['manifest_status']}"
    )
    print(
        f"  detector={report['detector_version']} "
        f"workflow={report['workflow_version']} "
        f"commit={report['commit_sha'] or 'unknown'}"
    )
    print(f"  manifest_hash={report['manifest_hash'][:16]}…")
    print(f"  corpus_role={report['corpus_role']}")
    if report["extraction_parser_developed_using_this_corpus"]:
        print(
            "  ** DEVELOPMENT CORPUS: these filings were inspected while the "
            "extraction parser\n"
            "  ** was written. Extraction results below are IN-SAMPLE and are "
            "NOT evidence that\n"
            "  ** extraction generalizes to unseen filings. "
            "generalization_claim_supported=false"
        )
    elif report["extraction_holdout_evaluation"]:
        claim = str(report["generalization_claim_supported"]).lower()
        print(
            "  ** EXTRACTION HOLDOUT EVALUATION: this corpus was frozen from "
            "official metadata\n"
            "  ** after the extraction parser was frozen, so the metrics below "
            "are OUT-OF-SAMPLE.\n"
            f"  ** generalization_claim_supported={claim}"
        )
        signoff = report.get("generalization_claim") or {}
        print(
            "  ** human sign-off: "
            + (
                f"{signoff['signoff_signer_id']} at "
                f"{signoff['signoff_signed_at_utc']}"
                if signoff.get("signoff_present")
                else "NONE RECORDED"
            )
        )
        for reason in report.get("generalization_claim_blocked_by", []):
            print(f"  **   blocked by: {reason}")

    quality = report["corpus_quality"]
    print("\nCorpus quality (counts):")
    for key in (
        "pairs_requested",
        "pairs_source_verified",
        "pairs_built",
        "pairs_extracted",
        "pairs_missing_section",
        "pairs_ambiguous_section",
        "pairs_parse_failed",
        "pairs_human_verified",
    ):
        print(f"  {key:<28} {quality[key]}")

    scope = report.get("scoring_scope")
    if scope:
        print("\nScoring scope (what the gold metrics cover):")
        for key in ("pairs_in_manifest", "pairs_scored", "pairs_excluded"):
            print(f"  {key:<28} {scope[key]}")
        for item in scope["excluded_pairs"]:
            print(
                f"  EXCLUDED {item['pair_id']:<19} [{item['code']}] "
                f"previous={item['previous_extraction_outcome']} "
                f"current={item['current_extraction_outcome']}"
            )
            print(f"    {item['detail']}")

    if report["mode"] == "unlabeled_execution":
        print()
        for warning in report["warnings"]:
            print(f"  ** {warning}")
        print("\nExecution outcomes:")
        for item in report["executions"]:
            if not item["built"]:
                print(f"  {item['pair_id']:<28} NOT BUILT")
                continue
            print(
                f"  {item['pair_id']:<28} "
                f"prev={item['previous_extraction_outcome']:<12} "
                f"curr={item['current_extraction_outcome']:<12} "
                f"changes={item['change_count']:<4} "
                f"undetermined={item['undetermined_count']:<4} "
                f"lifecycle={item['lifecycle'] or 'not-run'}"
            )
        operational = report["operational"]
        print(
            f"\nRuntime (percentile={operational['percentile_method']}, "
            f"n={operational['detection_duration_sample_size']}): "
            f"p50={operational['detection_duration_seconds_p50']} "
            f"p95={operational['detection_duration_seconds_p95']}"
        )
        print(
            f"  attempts={operational['total_detection_attempts']} "
            f"retries={operational['retries']} "
            f"jobs={operational['detection_jobs']} "
            f"reclaims={operational['lease_reclaims']}"
        )
        if operational["failures_by_code"]:
            print("  failures by code:")
            for code, count in operational["failures_by_code"].items():
                print(f"    {code}: {count}")
        return

    if report["refused"]:
        print("\nGOLD EVALUATION REFUSED — no accuracy metric is reported.")
        for reason in report["refusal_reasons"]:
            print(f"  [{reason['code']}] {reason['detail']}")
        print(f"\n{report['note']}")
        return

    print(f"\n{report['scope']}")
    print("\nGold metrics (numerator/denominator), human-verified pairs only:")
    for name, metric in report["gold_metrics"].items():
        _print_rate(name, metric)
    stats = report["label_statistics"]
    print(
        f"\nHuman-verified labels: {stats['human_verified_label_count']} "
        f"(confidence {stats['confidence_distribution']})"
    )
    operational = report["operational"]
    print(
        f"Runtime (percentile={operational['percentile_method']}): "
        f"p50={operational['detection_duration_seconds_p50']} "
        f"p95={operational['detection_duration_seconds_p95']}"
    )
    print(f"\n{report['threshold_policy']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate the Item 1A comparison workflow against the controlled "
            "real-filing benchmark. Offline: no network, no AWS, no LLM."
        )
    )
    parser.add_argument("--manifest", metavar="PATH", help="benchmark manifest path")
    parser.add_argument("--corpus-dir", metavar="PATH", help="local corpus directory")
    parser.add_argument(
        "--evaluation-config", metavar="PATH", help="declared evaluation config"
    )
    parser.add_argument(
        "--pair-id", action="append", default=[], help="evaluate only these pairs"
    )
    parser.add_argument(
        "--unlabeled",
        action="store_true",
        help="execution mechanics report with no accuracy metrics",
    )
    parser.add_argument(
        "--new-run",
        action="store_true",
        help=(
            "accept a detector/workflow version that differs from the declared "
            "evaluation config, creating an explicitly new run"
        ),
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--report", metavar="PATH", help="also write JSON to PATH")
    args = parser.parse_args(argv)

    try:
        # Which schema this manifest is decides which validator reads it, and
        # which corpus's evaluation config and corpus directory are the
        # defaults. Nothing is assumed to be the development corpus.
        evaluation_manifest = load_manifest_dispatch(args.manifest)
        config_document = load_evaluation_config(
            args.evaluation_config or evaluation_manifest.default_config_path
        )
        require_config_matches_manifest(
            config_document, evaluation_manifest.document
        )
        # Validated here so a malformed sign-off is a configuration failure
        # with a code, not a claim that quietly evaluates to false.
        validate_signoff_document(config_document)
    except rfb.BenchmarkError as exc:
        print(f"Invalid configuration [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2

    manifest = evaluation_manifest.document
    manifest_path = evaluation_manifest.path
    layout = rfb.CorpusLayout(
        args.corpus_dir or evaluation_manifest.default_corpus_dir
    )
    pairs = evaluation_manifest.pairs
    if args.pair_id:
        wanted = set(args.pair_id)
        unknown = sorted(wanted - {pair["pair_id"] for pair in pairs})
        if unknown:
            print(f"Unknown pair ids: {unknown}", file=sys.stderr)
            return 2
        pairs = [pair for pair in pairs if pair["pair_id"] in wanted]

    if not pairs:
        print(
            "The benchmark manifest has no resolved pairs. Its status is "
            f"{manifest['status']!r}: the corpus has not been acquired, built, "
            "annotated, or evaluated, and no real-filing metric exists.",
            file=sys.stderr,
        )
        return 2

    try:
        states = [collect_pair_state(pair, layout) for pair in pairs]
    except rfb.BenchmarkError as exc:
        print(f"Invalid configuration [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2

    if args.unlabeled:
        report = unlabeled_report(
            manifest, manifest_path, pairs, states, layout, config_document
        )
    else:
        report = gold_report(
            manifest,
            manifest_path,
            pairs,
            states,
            layout,
            config_document,
            new_run=args.new_run,
        )

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_report(report)

    if args.report:
        Path(args.report).write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )
        if not args.json:
            print(f"\nJSON report written to {Path(args.report).name}")

    return 1 if report.get("refused") else 0


if __name__ == "__main__":
    sys.exit(main())
