"""Gold evaluation of the extraction holdout, end to end through the CLI.

Two corpora now reach the same evaluator and they must not be confused for
each other. The development corpus (real_filing_v1) was inspected while the
Item heading parser was written, so results over it are in-sample. The holdout
(real_filing_holdout_v1) was frozen from official metadata after the parser was
frozen, so results over it are out-of-sample. Each has its own manifest schema,
its own evaluation config, and its own provenance block, and this suite pins
all three.

End-to-end coverage runs at two levels, because the real corpus is twenty SEC
filing bodies that cannot be committed:

* A SYNTHETIC holdout corpus, built in tmp_path through the real ingestion,
  extraction, and comparison path, drives the real CLI on every run including
  CI. It covers the whole path — dispatch, projection, config binding, scoring
  scope, the provenance block, and the sign-off gate — and never skips.
* The REAL corpus tests pin the published metric values. They skip when
  ``benchmark_data/`` is absent, and setting ``REQUIRE_REAL_FILING_HOLDOUT_CORPUS=1``
  turns that skip into a failure wherever the corpus was expected, because a
  skip reads as a pass on the summary line.

The generalization claim is the one output here that no computation may
produce. It is gated on a human sign-off recorded in the evaluation config, and
the tests below pin that a fully covered, out-of-sample, gold-scored run STILL
does not publish the claim without one.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path

import pytest

import config
import real_filing_benchmark as rfb
import real_filing_holdout as rfh
from scripts import eval_real_filing_benchmark as evaluator
from tests.helpers import real_filing_fixtures as fx

REPO_ROOT = Path(__file__).resolve().parent.parent
HOLDOUT_MANIFEST = REPO_ROOT / "benchmarks" / "real_filing_holdout_v1" / "manifest.json"
HOLDOUT_CONFIG = (
    REPO_ROOT / "benchmarks" / "real_filing_holdout_v1" / "evaluation_config.json"
)
DEV_MANIFEST = REPO_ROOT / "benchmarks" / "real_filing_v1" / "manifest.json"

HOLDOUT_CORPUS = Path(config.REAL_FILING_HOLDOUT_DIR)

#: The pair the blind extraction run left ambiguous on both sides. It carries
#: no review packet, so it can never be annotated and can never be scored.
BLOCKED_PAIR_ID = "sic-6000s-01"

_CORPUS_PRESENT = (HOLDOUT_CORPUS / "build").is_dir() and (
    HOLDOUT_CORPUS / "annotations"
).is_dir()

#: Set to "1" in any context where the real corpus is expected to exist — a
#: developer machine after acquisition, or a CI job that provisions it. A
#: skipped test reads as a pass on the summary line, so where these tests were
#: meant to run their absence has to be a failure instead of silence.
STRICT_ENV = "REQUIRE_REAL_FILING_HOLDOUT_CORPUS"

SKIP_REASON = (
    "the local holdout corpus under gitignored benchmark_data/ is not "
    "present; acquisition and the blind extraction run produce it. Set "
    f"{STRICT_ENV}=1 to make this a failure instead of a skip."
)


def _require_real_corpus():
    """Skip, or fail loudly where these tests were expected to run.

    CI covers this same CLI path against a synthetic holdout corpus built in
    tmp_path, so nothing here is the only coverage of the code under test —
    what skips is only the pinning of the REAL corpus's metric values.
    """
    if _CORPUS_PRESENT:
        return
    if os.environ.get(STRICT_ENV) == "1":
        pytest.fail(
            f"{STRICT_ENV}=1 declares that the real holdout corpus should be "
            f"present, but no build/ and annotations/ were found under "
            f"{HOLDOUT_CORPUS.name}/. These end-to-end tests did NOT run, and "
            "a skip would have reported as a pass."
        )
    pytest.skip(SKIP_REASON)


def _run_cli(argv):
    """Run the real CLI, capturing stdout. Returns (exit_code, stdout)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = evaluator.main(argv)
    return code, buffer.getvalue()


@pytest.fixture(scope="module")
def holdout_gold_report():
    _require_real_corpus()
    code, out = _run_cli(["--manifest", str(HOLDOUT_MANIFEST), "--json"])
    return {"code": code, "report": json.loads(out)}


@pytest.fixture(scope="module")
def holdout_gold_text():
    _require_real_corpus()
    code, out = _run_cli(["--manifest", str(HOLDOUT_MANIFEST)])
    return {"code": code, "out": out}


# --- End to end over the real holdout corpus ----------------------------------


def test_live_v3_workflow_refuses_to_rescore_the_frozen_v2_holdout(
    holdout_gold_report,
):
    """The frozen corpus is v2 evidence and the live workflow is v3: the gold
    CLI must REFUSE on the version gate rather than recompute metrics under an
    identity the committed config does not describe. The evaluation of record
    stays the committed gold_evaluation_report.json (byte-pinned by
    test_gold_evaluation_signoff), produced by item1a_detector.v2 before the
    v3 unit grammar existed."""
    assert holdout_gold_report["code"] == 1
    report = holdout_gold_report["report"]
    assert report["mode"] == "gold_evaluation"
    assert report["refused"] is True
    assert report["gold_metrics_available"] is False
    assert report["gold_metrics"] is None
    codes = sorted(reason["code"] for reason in report["refusal_reasons"])
    assert codes == ["detector_version_mismatch", "workflow_version_mismatch"]

    # The scoring scope stays visible even on refusal: the tenth pair's
    # exclusion is a fact about the corpus, not about this run.
    scope = report["scoring_scope"]
    assert scope["pairs_in_manifest"] == 10
    assert scope["pairs_scored"] == 9
    assert scope["pairs_excluded"] == 1
    assert scope["coverage_complete"] is False
    assert BLOCKED_PAIR_ID not in scope["scored_pair_ids"]
    assert len(scope["scored_pair_ids"]) == 9


def test_extraction_blocked_pair_is_reported_never_silently_dropped(
    holdout_gold_report, holdout_gold_text
):
    """The excluded pair is named, with the outcomes that excluded it.

    A denominator of nine out of ten frozen pairs is only honest if the tenth
    is visible. It must appear in the structured report AND in the printed
    output, not merely be absent from the metrics.
    """
    scope = holdout_gold_report["report"]["scoring_scope"]
    assert len(scope["excluded_pairs"]) == 1
    excluded = scope["excluded_pairs"][0]
    assert excluded["pair_id"] == BLOCKED_PAIR_ID
    assert excluded["code"] == evaluator.EXCLUSION_EXTRACTION_BLOCKED
    assert excluded["previous_extraction_outcome"] == rfb.EXTRACTION_AMBIGUOUS
    assert excluded["current_extraction_outcome"] == rfb.EXTRACTION_AMBIGUOUS
    assert excluded["detail"]

    out = holdout_gold_text["out"]
    assert "Scoring scope" in out
    assert BLOCKED_PAIR_ID in out
    assert evaluator.EXCLUSION_EXTRACTION_BLOCKED in out


def test_holdout_run_carries_positive_holdout_provenance(holdout_gold_report):
    """The affirmative values, not merely the absence of a warning banner.

    A report that simply omitted the development banner would look identical
    to one whose corpus role was never resolved, so even a refused run has to
    state what the corpus IS — and what this run did not do.
    """
    report = holdout_gold_report["report"]
    assert report["corpus_role"] == rfb.CORPUS_ROLE_EXTRACTION_HOLDOUT
    # This refused run performed no evaluation, and its provenance says so —
    # corpus identity (still the holdout) and run lifecycle (no evaluation
    # here) are separate facts. The committed v2 report keeps recording the
    # evaluation that DID happen.
    assert report["extraction_holdout_evaluation"] is False
    assert report["extraction_parser_developed_using_this_corpus"] is False
    assert report["benchmark_id"] == rfh.HOLDOUT_BENCHMARK_ID
    assert report["corpus_role_detail"]
    committed = json.loads(
        (
            REPO_ROOT
            / "benchmarks"
            / "real_filing_holdout_v1"
            / "gold_evaluation_report.json"
        ).read_text(encoding="utf-8")
    )
    assert committed["extraction_holdout_evaluation"] is True
    assert committed["detector_version"] == "item1a_detector.v2"


def test_holdout_run_does_not_claim_generalization_while_coverage_is_partial(
    holdout_gold_report,
):
    """Out-of-sample is not the same as generalizing, and the gap is stated.

    The committed holdout config records no sign-off, so the real corpus's
    published claim is false and says so by naming the missing sign-off — not
    by pointing at coverage, which is reported separately.
    """
    report = holdout_gold_report["report"]
    assert report["generalization_claim_supported"] is False

    # The committed evaluation of record still publishes the full claim block,
    # blocked on the missing human sign-off — the live refusal changes none of
    # that.
    committed = json.loads(
        (
            REPO_ROOT
            / "benchmarks"
            / "real_filing_holdout_v1"
            / "gold_evaluation_report.json"
        ).read_text(encoding="utf-8")
    )
    assert committed["generalization_claim_supported"] is False
    claim = committed["generalization_claim"]
    assert claim["supported"] is False
    assert claim["signoff_present"] is False
    assert claim["signoff_signer_id"] is None
    assert any("sign-off" in reason for reason in claim["blocked_by"])
    # Coverage travels beside the claim as its own reported fact.
    assert claim["coverage_complete"] is False
    assert claim["pairs_scored"] == 9
    assert claim["pairs_in_manifest"] == 10


def test_holdout_gold_metric_values_are_pinned(holdout_gold_report):
    """The published v2 values stay pinned at the numerator/denominator.

    These are the values the frozen corpus, frozen v2 detector, and nine
    human-verified annotations produced. The live v3 workflow can no longer
    recompute them (the version gate refuses), so the pin holds against the
    committed report — the only evaluation of record.
    """
    assert holdout_gold_report["report"]["gold_metrics"] is None
    committed = json.loads(
        (
            REPO_ROOT
            / "benchmarks"
            / "real_filing_holdout_v1"
            / "gold_evaluation_report.json"
        ).read_text(encoding="utf-8")
    )
    metrics = committed["gold_metrics"]
    expected = {
        "change_precision": (11, 24),
        "change_recall": (11, 21),
        "change_type_accuracy": (11, 21),
        "unchanged_false_positive_rate": (4, 7),
        "evidence_resolution_rate": (1570, 1570),
        "pair_exact_match_rate": (1, 9),
    }
    for name, (numerator, denominator) in expected.items():
        assert metrics[name]["numerator"] == numerator, name
        assert metrics[name]["denominator"] == denominator, name
        assert metrics[name]["zero_denominator"] is False, name

    # No labelled undetermined reasons or directions exist in this corpus, so
    # these assert nothing and must say so rather than report a rate.
    for name in ("undetermined_reason_accuracy", "direction_consistency_accuracy"):
        assert metrics[name]["denominator"] == 0, name
        assert metrics[name]["value"] is None, name
        assert metrics[name]["zero_denominator"] is True, name


def test_holdout_gold_run_uses_the_holdout_config_by_default(holdout_gold_report):
    """No --evaluation-config was passed, so the corpus chose its own."""
    document = json.loads(HOLDOUT_CONFIG.read_text(encoding="utf-8"))
    assert document["benchmark_id"] == rfh.HOLDOUT_BENCHMARK_ID
    report = holdout_gold_report["report"]
    assert report["declared_detector_version"] == document["declared_detector_version"]
    assert report["declared_workflow_version"] == document["declared_workflow_version"]


def test_holdout_report_leaks_no_local_path_or_filing_text(holdout_gold_text):
    out = holdout_gold_text["out"]
    assert str(HOLDOUT_CORPUS.resolve()) not in out
    assert "<html" not in out.lower()


# --- The development corpus keeps its own identity ----------------------------


def test_development_corpus_keeps_its_own_banner_and_provenance():
    """Requirement's companion: the dev path is untouched by holdout support.

    The committed development manifest still refuses gold (it has no verified
    labels), and its report still identifies an in-sample corpus. Provenance is
    emitted on the refusal path too, so this needs no built corpus.
    """
    code, text = _run_cli(["--manifest", str(DEV_MANIFEST)])
    assert code == 1
    assert "DEVELOPMENT CORPUS" in text
    assert "IN-SAMPLE" in text
    assert "EXTRACTION HOLDOUT EVALUATION" not in text

    code, raw = _run_cli(["--manifest", str(DEV_MANIFEST), "--json"])
    report = json.loads(raw)
    assert code == 1
    assert report["benchmark_id"] == "real_filing_v1"
    assert report["corpus_role"] == rfb.CORPUS_ROLE_EXTRACTION_DEVELOPMENT
    assert report["extraction_parser_developed_using_this_corpus"] is True
    assert report["extraction_holdout_evaluation"] is False
    assert report["generalization_claim_supported"] is False


def test_a_refused_run_never_claims_a_holdout_evaluation():
    """Refusing is not evaluating, whichever corpus was pointed at."""
    _code, raw = _run_cli(["--manifest", str(DEV_MANIFEST), "--json"])
    report = json.loads(raw)
    assert report["refused"] is True
    assert report["extraction_holdout_evaluation"] is False


# --- Strict schema dispatch ---------------------------------------------------


def test_development_branch_rejects_the_holdout_manifest():
    """The dev branch reads its schema only. It never learns holdout keys."""
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        evaluator.load_development_manifest(HOLDOUT_MANIFEST)
    assert excinfo.value.code == "manifest_schema_dispatch_mismatch"

    # And the underlying validator refuses it independently of dispatch, so
    # neither layer is the only thing standing between the two schemas.
    with pytest.raises(rfb.ManifestSchemaError):
        rfb.load_manifest(HOLDOUT_MANIFEST)


def test_holdout_branch_rejects_the_development_manifest():
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        evaluator.load_holdout_manifest(DEV_MANIFEST)
    assert excinfo.value.code == "manifest_schema_dispatch_mismatch"

    with pytest.raises(rfh.HoldoutManifestError):
        rfh.load_holdout_manifest(DEV_MANIFEST)


def test_dispatch_selects_a_branch_from_schema_version_alone():
    dev = evaluator.load_manifest_dispatch(DEV_MANIFEST)
    assert dev.schema_version == evaluator.DEV_MANIFEST_SCHEMA
    assert dev.corpus_role == rfb.CORPUS_ROLE_EXTRACTION_DEVELOPMENT
    assert dev.default_config_path == evaluator.DEFAULT_EVALUATION_CONFIG

    holdout = evaluator.load_manifest_dispatch(HOLDOUT_MANIFEST)
    assert holdout.schema_version == evaluator.HOLDOUT_MANIFEST_SCHEMA
    assert holdout.corpus_role == rfb.CORPUS_ROLE_EXTRACTION_HOLDOUT
    assert holdout.default_config_path == evaluator.DEFAULT_HOLDOUT_EVALUATION_CONFIG


def test_dispatch_refuses_an_unknown_schema_version(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"schema_version": "some.other.v9"}), encoding="utf-8")
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        evaluator.load_manifest_dispatch(path)
    assert excinfo.value.code == "manifest_schema_version_unsupported"


def test_dispatch_refuses_a_manifest_with_no_schema_version(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"benchmark_id": "x"}), encoding="utf-8")
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        evaluator.load_manifest_dispatch(path)
    assert excinfo.value.code == "manifest_schema_version_missing"


# --- Pair projection ----------------------------------------------------------


def test_holdout_pairs_project_stratum_label_onto_sector_label():
    """The holdout's SIC stratum is the evaluator's sector label.

    The projection happens here, at read time. Neither frozen schema grows the
    other's key, and the manifest itself is not mutated to suit the reader.
    """
    document = rfh.load_holdout_manifest(HOLDOUT_MANIFEST)
    projected = evaluator.holdout_evaluation_pairs(document)
    assert [pair["sector_label"] for pair in projected] == [
        pair["stratum_label"] for pair in document["pairs"]
    ]
    assert all(pair["sector_label"] for pair in projected)
    for pair in document["pairs"]:
        assert "sector_label" not in pair


def test_projected_pairs_carry_what_checksum_verification_needs():
    document = rfh.load_holdout_manifest(HOLDOUT_MANIFEST)
    for pair in evaluator.holdout_evaluation_pairs(document):
        for _side, payload in rfb.pair_sides(pair):
            assert len(payload["expected_sha256"]) == 64


def test_missing_sector_label_is_a_stated_error_not_a_keyerror(tmp_path):
    """The bare pair["sector_label"] read used to raise an uncaught KeyError.

    A traceback is not a refusal: it names no code and states no reason. An
    incomplete projection has to fail the way every other configuration defect
    does.
    """
    layout = rfb.CorpusLayout(tmp_path)
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        evaluator.collect_pair_state(
            {"pair_id": "sic-0000s-01", "issuer_name": "EXAMPLE"}, layout
        )
    assert excinfo.value.code == "evaluation_pair_missing_field"
    assert "sector_label" in excinfo.value.message


# --- Evaluation config binding ------------------------------------------------


def test_config_must_name_the_corpus_it_is_evaluating():
    document = json.loads(HOLDOUT_CONFIG.read_text(encoding="utf-8"))
    manifest = rfh.load_holdout_manifest(HOLDOUT_MANIFEST)
    evaluator.require_config_matches_manifest(document, manifest)

    mismatched = dict(document)
    mismatched["benchmark_id"] = "real_filing_v1"
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        evaluator.require_config_matches_manifest(mismatched, manifest)
    assert excinfo.value.code == "evaluation_config_benchmark_mismatch"


def test_cli_refuses_the_wrong_corpus_config(capsys):
    """Pointing the holdout run at the development config exits 2."""
    code = evaluator.main(
        [
            "--manifest",
            str(HOLDOUT_MANIFEST),
            "--evaluation-config",
            str(evaluator.DEFAULT_EVALUATION_CONFIG),
        ]
    )
    assert code == 2
    assert "evaluation_config_benchmark_mismatch" in capsys.readouterr().err


# --- Synthetic holdout corpus: the same CLI path, runnable in CI ---------------
#
# These tests need no gitignored corpus. They build a complete holdout-schema
# corpus in tmp_path through the real ingestion, extraction, and comparison
# path, then drive the real CLI over it — so the provenance block, the scoring
# scope, and the sign-off gate are all covered on every CI run rather than
# skipped with the corpus that cannot be committed.


@pytest.fixture(scope="module")
def synthetic_corpus(tmp_path_factory):
    return fx.build_synthetic_holdout_corpus(tmp_path_factory.mktemp("holdout"))


def _run_synthetic(corpus, tmp_path, *, signoff=None, extra=()):
    config_path = fx.write_holdout_evaluation_config(tmp_path, signoff=signoff)
    code, raw = _run_cli(
        [
            "--manifest",
            str(corpus["manifest_path"]),
            "--corpus-dir",
            str(corpus["layout"].root),
            "--evaluation-config",
            str(config_path),
            "--json",
            *extra,
        ]
    )
    return code, json.loads(raw)


def test_synthetic_holdout_cli_runs_end_to_end(synthetic_corpus, tmp_path):
    """The real CLI, the real pipeline, no committed corpus required."""
    code, report = _run_synthetic(synthetic_corpus, tmp_path)
    assert code == 0
    assert report["refused"] is False
    assert report["gold_metrics_available"] is True
    assert report["benchmark_id"] == rfh.HOLDOUT_BENCHMARK_ID

    scope = report["scoring_scope"]
    assert scope["pairs_in_manifest"] == rfh.TARGET_PAIR_COUNT
    assert scope["pairs_scored"] == len(synthetic_corpus["scored_pair_ids"])
    assert scope["pairs_excluded"] == len(synthetic_corpus["blocked_pair_ids"])
    assert scope["excluded_pairs"][0]["pair_id"] == (
        synthetic_corpus["blocked_pair_ids"][0]
    )
    assert scope["excluded_pairs"][0]["code"] == (
        evaluator.EXCLUSION_EXTRACTION_BLOCKED
    )


def test_synthetic_holdout_run_carries_positive_holdout_provenance(
    synthetic_corpus, tmp_path
):
    """The affirmative provenance values, asserted where CI can see them."""
    _code, report = _run_synthetic(synthetic_corpus, tmp_path)
    assert report["corpus_role"] == rfb.CORPUS_ROLE_EXTRACTION_HOLDOUT
    assert report["extraction_holdout_evaluation"] is True
    assert report["extraction_parser_developed_using_this_corpus"] is False


def test_synthetic_holdout_text_output_shows_the_holdout_banner(
    synthetic_corpus, tmp_path
):
    config_path = fx.write_holdout_evaluation_config(tmp_path)
    code, out = _run_cli(
        [
            "--manifest",
            str(synthetic_corpus["manifest_path"]),
            "--corpus-dir",
            str(synthetic_corpus["layout"].root),
            "--evaluation-config",
            str(config_path),
        ]
    )
    assert code == 0
    assert "EXTRACTION HOLDOUT EVALUATION" in out
    assert "OUT-OF-SAMPLE" in out
    assert "DEVELOPMENT CORPUS" not in out
    assert "Scoring scope" in out


# --- The sign-off gate --------------------------------------------------------


def test_claim_is_false_without_a_signoff(synthetic_corpus, tmp_path):
    _code, report = _run_synthetic(synthetic_corpus, tmp_path, signoff=None)
    claim = report["generalization_claim"]
    assert claim["supported"] is False
    assert report["generalization_claim_supported"] is False
    assert claim["signoff_present"] is False
    assert any("sign-off" in reason for reason in claim["blocked_by"])


def test_complete_coverage_alone_never_grants_the_claim(tmp_path_factory, tmp_path):
    """The load-bearing test for this gate.

    A corpus where every frozen pair scored — coverage_complete true, holdout
    role, gold metrics produced — still does not support the claim, because no
    human signed it. Coverage is reported; it does not authorize.
    """
    corpus = fx.build_synthetic_holdout_corpus(
        tmp_path_factory.mktemp("full-coverage"), block_last_pair=False
    )
    assert corpus["blocked_pair_ids"] == []

    _code, report = _run_synthetic(corpus, tmp_path, signoff=None)
    scope = report["scoring_scope"]
    assert scope["pairs_scored"] == rfh.TARGET_PAIR_COUNT
    assert scope["coverage_complete"] is True

    claim = report["generalization_claim"]
    assert claim["coverage_complete"] is True
    assert claim["supported"] is False
    assert report["generalization_claim_supported"] is False
    assert any("sign-off" in reason for reason in claim["blocked_by"])


def test_a_valid_signoff_publishes_the_claim(synthetic_corpus, tmp_path):
    """The only path to a true claim: a person recorded as having made it."""
    signoff = fx.holdout_signoff(
        manifest_sha256=synthetic_corpus["manifest_sha256"],
        pairs_scored=len(synthetic_corpus["scored_pair_ids"]),
    )
    _code, report = _run_synthetic(synthetic_corpus, tmp_path, signoff=signoff)
    claim = report["generalization_claim"]
    assert claim["supported"] is True
    assert claim["blocked_by"] == []
    assert claim["signoff_present"] is True
    assert claim["signoff_signer_id"] == fx.HOLDOUT_SIGNER
    assert claim["signoff_signed_at_utc"] == fx.HOLDOUT_SIGNED_AT
    assert report["generalization_claim_supported"] is True
    # Corpus identity is untouched by the claim.
    assert report["extraction_parser_developed_using_this_corpus"] is False


def test_signoff_naming_another_manifest_does_not_authorize(
    synthetic_corpus, tmp_path
):
    """A sign-off is bound to the corpus state it was given."""
    signoff = fx.holdout_signoff(
        manifest_sha256="a" * 64,
        pairs_scored=len(synthetic_corpus["scored_pair_ids"]),
    )
    _code, report = _run_synthetic(synthetic_corpus, tmp_path, signoff=signoff)
    claim = report["generalization_claim"]
    assert claim["supported"] is False
    assert any("different manifest" in reason for reason in claim["blocked_by"])


def test_signoff_acknowledging_other_coverage_does_not_authorize(
    synthetic_corpus, tmp_path
):
    """The signer must have seen the coverage this run actually produced."""
    signoff = fx.holdout_signoff(
        manifest_sha256=synthetic_corpus["manifest_sha256"],
        pairs_scored=len(synthetic_corpus["scored_pair_ids"]) + 1,
    )
    _code, report = _run_synthetic(synthetic_corpus, tmp_path, signoff=signoff)
    claim = report["generalization_claim"]
    assert claim["supported"] is False
    assert any("did not see this coverage" in r for r in claim["blocked_by"])


@pytest.mark.parametrize(
    "signer", ["", "  ", "TODO", "changeme", "unknown", "anonymous"]
)
def test_signoff_must_identify_a_person(synthetic_corpus, tmp_path, signer):
    signoff = fx.holdout_signoff(
        manifest_sha256=synthetic_corpus["manifest_sha256"],
        pairs_scored=len(synthetic_corpus["scored_pair_ids"]),
        signer_id=signer,
    )
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        evaluator.validate_signoff_document(
            {evaluator.SIGNOFF_FIELD: signoff}
        )
    assert excinfo.value.code == "generalization_signoff_unattributed"


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda s: s.pop("signer_id"), "generalization_signoff_missing_keys"),
        (lambda s: s.pop("signed_at_utc"), "generalization_signoff_missing_keys"),
        (lambda s: s.pop("manifest_sha256"), "generalization_signoff_missing_keys"),
        (
            lambda s: s.pop("acknowledged_pairs_scored"),
            "generalization_signoff_missing_keys",
        ),
        (lambda s: s.update(extra=1), "generalization_signoff_unknown_keys"),
        (
            lambda s: s.update(signed_at_utc="March 2026"),
            "generalization_signoff_invalid_timestamp",
        ),
        (
            lambda s: s.update(manifest_sha256="not-a-digest"),
            "generalization_signoff_invalid_manifest_hash",
        ),
        (
            lambda s: s.update(acknowledged_pairs_scored="nine"),
            "generalization_signoff_invalid_coverage",
        ),
    ],
)
def test_a_malformed_signoff_is_a_configuration_error(mutate, code):
    """A broken sign-off must not degrade quietly to an unsigned run.

    If it did, a typo in the signer's block would be indistinguishable from a
    deliberate decision not to sign.
    """
    signoff = fx.holdout_signoff(manifest_sha256="b" * 64, pairs_scored=9)
    mutate(signoff)
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        evaluator.validate_signoff_document({evaluator.SIGNOFF_FIELD: signoff})
    assert excinfo.value.code == code


def test_a_non_object_signoff_is_rejected():
    for value in ("signed", 1, ["signed"], True):
        with pytest.raises(rfb.BenchmarkError) as excinfo:
            evaluator.validate_signoff_document({evaluator.SIGNOFF_FIELD: value})
        assert excinfo.value.code == "generalization_signoff_malformed"


def test_cli_exits_two_on_a_malformed_signoff(synthetic_corpus, tmp_path, capsys):
    config_path = fx.write_holdout_evaluation_config(
        tmp_path, signoff={"signer_id": "someone"}
    )
    code = evaluator.main(
        [
            "--manifest",
            str(synthetic_corpus["manifest_path"]),
            "--corpus-dir",
            str(synthetic_corpus["layout"].root),
            "--evaluation-config",
            str(config_path),
        ]
    )
    assert code == 2
    assert "generalization_signoff_missing_keys" in capsys.readouterr().err


def test_committed_holdout_config_records_no_signoff():
    """The artifact of record is unsigned, and that is the honest state."""
    document = json.loads(HOLDOUT_CONFIG.read_text(encoding="utf-8"))
    assert evaluator.SIGNOFF_FIELD in document
    assert document[evaluator.SIGNOFF_FIELD] is None


def test_no_tool_in_the_repository_writes_a_signoff():
    """Same contract as human_verified on an annotation: no writer exists.

    A sign-off that some script could stamp would not be a human judgement. The
    only places the field may be assigned are the evaluator's own validator and
    the test fixtures that construct one.
    """
    allowed = {
        Path("scripts/eval_real_filing_benchmark.py"),
        Path("tests/helpers/real_filing_fixtures.py"),
        Path("tests/test_real_filing_holdout_gold_evaluation.py"),
    }
    offenders = []
    for path in REPO_ROOT.rglob("*.py"):
        relative = path.relative_to(REPO_ROOT)
        if relative in allowed or ".venv" in relative.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if evaluator.SIGNOFF_FIELD in text:
            offenders.append(str(relative))
    assert offenders == [], offenders


# --- Strict-mode guard --------------------------------------------------------


def test_absent_corpus_fails_instead_of_skipping_when_declared(monkeypatch):
    """A skip reads as a pass, so strict mode must turn it into a failure."""
    monkeypatch.setenv(STRICT_ENV, "1")
    monkeypatch.setattr(
        "tests.test_real_filing_holdout_gold_evaluation._CORPUS_PRESENT", False
    )
    with pytest.raises(BaseException, match="did NOT run"):
        _require_real_corpus()


def test_absent_corpus_skips_when_not_declared(monkeypatch):
    monkeypatch.delenv(STRICT_ENV, raising=False)
    monkeypatch.setattr(
        "tests.test_real_filing_holdout_gold_evaluation._CORPUS_PRESENT", False
    )
    with pytest.raises(BaseException, match="not\npresent|not present"):
        _require_real_corpus()


# --- The committed gold report ------------------------------------------------
#
# The artifact of record for real_filing_holdout_v1. Unlike the blind-extraction
# reports beside it, this one DOES describe a holdout evaluation, so it is the
# one committed artifact whose extraction_holdout_evaluation is true. What it
# still does not carry is a generalization claim.

GOLD_REPORT = (
    REPO_ROOT / "benchmarks" / "real_filing_holdout_v1" / "gold_evaluation_report.json"
)


@pytest.fixture(scope="module")
def committed_gold_report():
    return json.loads(GOLD_REPORT.read_text(encoding="utf-8"))


def test_committed_gold_report_exists_and_is_a_completed_evaluation(
    committed_gold_report,
):
    assert committed_gold_report["mode"] == "gold_evaluation"
    assert committed_gold_report["refused"] is False
    assert committed_gold_report["gold_metrics_available"] is True
    assert committed_gold_report["benchmark_id"] == rfh.HOLDOUT_BENCHMARK_ID


def test_committed_gold_report_provenance_block(committed_gold_report):
    """The whole point of the artifact: what corpus, evaluated how."""
    assert committed_gold_report["corpus_role"] == (
        rfb.CORPUS_ROLE_EXTRACTION_HOLDOUT
    )
    assert committed_gold_report["extraction_parser_developed_using_this_corpus"] is (
        False
    )
    assert committed_gold_report["extraction_holdout_evaluation"] is True
    assert committed_gold_report["generalization_claim_supported"] is False
    assert committed_gold_report["detector_version"] == "item1a_detector.v2"
    assert committed_gold_report["workflow_version"] == "comparison_workflow.v2"


def test_committed_gold_report_pins_the_manifest_it_evaluated(committed_gold_report):
    assert committed_gold_report["manifest_hash"] == rfb.sha256_file(HOLDOUT_MANIFEST)
    assert committed_gold_report["manifest_status"] == "corpus_built"


def test_committed_gold_report_metric_values(committed_gold_report):
    """The published numbers, pinned in the committed artifact itself."""
    metrics = committed_gold_report["gold_metrics"]
    assert (metrics["change_precision"]["numerator"], metrics["change_precision"]["denominator"]) == (11, 24)
    assert (metrics["change_recall"]["numerator"], metrics["change_recall"]["denominator"]) == (11, 21)
    assert (metrics["pair_exact_match_rate"]["numerator"], metrics["pair_exact_match_rate"]["denominator"]) == (1, 9)


def test_committed_gold_report_states_its_scope_and_exclusion(committed_gold_report):
    scope = committed_gold_report["scoring_scope"]
    assert scope["pairs_in_manifest"] == 10
    assert scope["pairs_scored"] == 9
    assert scope["pairs_excluded"] == 1
    assert scope["coverage_complete"] is False
    assert scope["excluded_pairs"][0]["pair_id"] == BLOCKED_PAIR_ID


def test_committed_gold_report_records_no_signoff(committed_gold_report):
    claim = committed_gold_report["generalization_claim"]
    assert claim["supported"] is False
    assert claim["signoff_present"] is False
    assert claim["signoff_signer_id"] is None
    assert any("sign-off" in reason for reason in claim["blocked_by"])


def test_committed_gold_report_leaks_nothing(committed_gold_report):
    raw = GOLD_REPORT.read_text(encoding="utf-8")
    lowered = raw.lower()
    for forbidden in ("/users/", "/home/", "c:\\", "/archives/", "<html", "sec_user_agent"):
        assert forbidden not in lowered, forbidden
    # Reviewer notes are review context for a person, never an output.
    assert committed_gold_report["label_statistics"]["reviewer_notes_included"] is False


def test_committed_gold_report_does_not_overclaim():
    """Prose check, matching the convention the other committed reports use."""
    raw = GOLD_REPORT.read_text(encoding="utf-8").lower()
    for phrase in (
        "proves generaliz",
        "demonstrates generaliz",
        "generalizes to",
        "is unbiased",
    ):
        assert phrase not in raw, phrase
    assert '"generalization_claim_supported": false' in raw
