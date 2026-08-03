"""The generalization sign-off statement gate.

A gold evaluation can be complete, out-of-sample, fully covered, and scored
against human-verified labels and still not support a generalization claim.
That claim is a human judgement, and the only thing in the sign-off block that
records the judgement itself — rather than who acted and when — is the
statement the signer wrote. Identity and a timestamp say a person was there.
A statement says what they asserted.

The statement used to be optional, so a sign-off carrying identity, a
timestamp, and two hashes could publish the claim with no sentence attached to
it. This suite pins the strict contract that replaced that, and pins equally
hard that the committed evaluation of record did not change: it remains
unsigned, its metrics remain what they were, and
``generalization_claim_supported`` remains false.

Everything here is offline and synthetic: no network, no AWS, no SEC endpoint,
no filing body, no gold label. The committed artifacts are read byte-for-byte
and never written.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import re
from pathlib import Path

import pytest
import yaml

import real_filing_benchmark as rfb
from scripts import eval_real_filing_benchmark as evaluator
from tests.helpers import real_filing_fixtures as fx

REPO_ROOT = Path(__file__).resolve().parent.parent
HOLDOUT_DIR = REPO_ROOT / "benchmarks" / "real_filing_holdout_v1"
GOLD_REPORT = HOLDOUT_DIR / "gold_evaluation_report.json"
HOLDOUT_MANIFEST = HOLDOUT_DIR / "manifest.json"
HOLDOUT_CONFIG = HOLDOUT_DIR / "evaluation_config.json"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "comparison-regression.yml"
THIS_SUITE = "tests/test_gold_evaluation_signoff.py"

README = REPO_ROOT / "README.MD"
BENCHMARK_DOC = REPO_ROOT / "BENCHMARK.md"
WRITEUP = REPO_ROOT / "HOLDOUT_EVALUATION.md"
DOCS = (README, BENCHMARK_DOC)

#: The writeup is the detailed narrative and is frozen by this commit too.
WRITEUP_SHA256 = "10a0b4ca10fb1a5a565355ae22b8539cde5c2f4c56b946c6ea9ba3c41a739c1c"

#: The evaluation of record, frozen when it was merged. Recomputed from the
#: file on every run, so any edit to the committed report fails this suite.
GOLD_REPORT_SHA256 = "e5d2bdae54a8c2eec6fbd554d26602730a16482eea05713f0489b4d2949d1291"

#: Every metric the frozen report published, with the exact value and the exact
#: fraction it published. A second, independent copy of the numbers, so a
#: regeneration that preserved the file's shape but moved a value still fails.
FROZEN_GOLD_METRICS = {
    "change_precision": (0.458333, 11, 24),
    "change_recall": (0.52381, 11, 21),
    "change_type_accuracy": (0.52381, 11, 21),
    "unchanged_false_positive_rate": (0.571429, 4, 7),
    "evidence_resolution_rate": (1.0, 1570, 1570),
    "pair_exact_match_rate": (0.111111, 1, 9),
    "undetermined_reason_accuracy": (None, 0, 0),
    "direction_consistency_accuracy": (None, 0, 0),
}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def committed_gold_report() -> dict:
    return json.loads(GOLD_REPORT.read_text(encoding="utf-8"))


def _signoff(**overrides) -> dict:
    """A well-formed synthetic sign-off block. Only a test constructs one."""
    block = fx.holdout_signoff(manifest_sha256="b" * 64, pairs_scored=9)
    block.update(overrides)
    return block


def _validate(signoff) -> dict | None:
    return evaluator.validate_signoff_document({evaluator.SIGNOFF_FIELD: signoff})


def _claim(signoff, **overrides) -> dict:
    """Run the claim gate over a synthetic config document."""
    kwargs = {
        "manifest_sha256": "b" * 64,
        "holdout_evaluation_performed": True,
        "pairs_scored": 9,
        "coverage_complete": True,
        "pairs_in_manifest": 10,
    }
    kwargs.update(overrides)
    return evaluator.evaluate_generalization_claim(
        {evaluator.SIGNOFF_FIELD: signoff}, **kwargs
    )


# --- Legacy compatibility -----------------------------------------------------
#
# The committed evaluation is historical evidence. Hardening the admission rule
# for a FUTURE sign-off must not reinterpret, regenerate, or revalue it.


def test_committed_gold_report_is_byte_identical():
    assert _sha256_file(GOLD_REPORT) == GOLD_REPORT_SHA256


def test_committed_gold_report_is_unsigned(committed_gold_report):
    claim = committed_gold_report["generalization_claim"]
    assert claim["signoff_present"] is False
    assert claim["signoff_signer_id"] is None
    assert claim["signoff_signed_at_utc"] is None
    assert claim["signoff_statement"] is None


def test_committed_gold_report_claims_no_generalization(committed_gold_report):
    assert committed_gold_report["generalization_claim_supported"] is False
    assert committed_gold_report["generalization_claim"]["supported"] is False
    assert committed_gold_report["generalization_claim_blocked_by"]


def test_committed_gold_report_metrics_are_unchanged(committed_gold_report):
    """The numbers, pinned independently of the file hash."""
    metrics = committed_gold_report["gold_metrics"]
    assert set(metrics) == set(FROZEN_GOLD_METRICS)
    for name, (value, numerator, denominator) in FROZEN_GOLD_METRICS.items():
        assert metrics[name]["value"] == value, name
        assert metrics[name]["numerator"] == numerator, name
        assert metrics[name]["denominator"] == denominator, name


def test_committed_gold_report_key_paths_are_unchanged(committed_gold_report):
    """The claim's key path is part of the frozen contract, not just its value."""
    assert "generalization_claim_supported" in committed_gold_report
    for field in (
        "supported",
        "blocked_by",
        "coverage_complete",
        "pairs_scored",
        "pairs_in_manifest",
        "signoff_present",
        "signoff_signer_id",
        "signoff_signed_at_utc",
        "signoff_statement",
        "policy",
    ):
        assert field in committed_gold_report["generalization_claim"], field


def test_committed_gold_report_still_reports_a_completed_evaluation(
    committed_gold_report,
):
    """Complete but unsigned: the two facts this commit keeps separate."""
    assert committed_gold_report["refused"] is False
    assert committed_gold_report["gold_metrics_available"] is True
    assert committed_gold_report["extraction_holdout_evaluation"] is True
    assert committed_gold_report["generalization_claim_supported"] is False


def test_legacy_unsigned_config_still_loads_and_stays_unsigned():
    """A config written before the statement was required must still parse.

    Both committed configs record a null sign-off, which is the legacy shape.
    It resolves to unsigned — never to affirmative, and never to a hard error.
    """
    document = json.loads(HOLDOUT_CONFIG.read_text(encoding="utf-8"))
    assert document[evaluator.SIGNOFF_FIELD] is None
    assert evaluator.validate_signoff_document(document) is None

    claim = evaluator.evaluate_generalization_claim(
        document,
        manifest_sha256=rfb.manifest_hash(HOLDOUT_MANIFEST),
        holdout_evaluation_performed=True,
        pairs_scored=9,
        coverage_complete=False,
        pairs_in_manifest=10,
    )
    assert claim["supported"] is False
    assert claim["signoff_present"] is False
    assert claim["signoff_statement"] is None


def test_legacy_missing_signoff_key_never_implies_affirmative():
    """An older config that lacks the key entirely is unsigned, not signed."""
    assert evaluator.validate_signoff_document({}) is None
    claim = evaluator.evaluate_generalization_claim(
        {},
        manifest_sha256="b" * 64,
        holdout_evaluation_performed=True,
        pairs_scored=9,
        coverage_complete=True,
        pairs_in_manifest=10,
    )
    assert claim["supported"] is False


def test_legacy_null_signoff_never_implies_affirmative():
    assert _validate(None) is None
    assert _claim(None)["supported"] is False


def test_committed_holdout_artifacts_are_byte_identical():
    """Every committed holdout artifact this evaluation rests on."""
    for name in (
        "manifest.json",
        "evaluation_config.json",
        "gold_evaluation_report.json",
        "blind_extraction_report.json",
        "execution_report.json",
        "annotation_packet_inventory.json",
        "source_verification_report.json",
        "selection_report.json",
    ):
        path = HOLDOUT_DIR / name
        assert path.is_file(), name
        document = json.loads(path.read_text(encoding="utf-8"))
        # Not one of them acquired a sign-off or a claim in this commit.
        assert document.get("generalization_claim_supported") is not True, name


# --- Strict statement validation ----------------------------------------------


def test_missing_statement_is_rejected_for_an_affirmative_signoff():
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        evaluator.validate_signoff_statement()
    assert excinfo.value.code == evaluator.SIGNOFF_STATEMENT_REQUIRED


def test_signoff_block_without_a_statement_is_rejected():
    """The exact hole this commit closes."""
    block = _signoff()
    del block[evaluator.STATEMENT_FIELD]
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        _validate(block)
    assert excinfo.value.code == evaluator.SIGNOFF_STATEMENT_REQUIRED


def test_null_statement_is_rejected():
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        evaluator.validate_signoff_statement(None)
    assert excinfo.value.code == evaluator.SIGNOFF_STATEMENT_INVALID_TYPE


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        0,
        1,
        1.5,
        [],
        ["I reviewed this"],
        {},
        {"statement": "I reviewed this"},
        b"I reviewed this",
        bytearray(b"I reviewed this"),
        ("I reviewed this",),
        set(),
    ],
    ids=repr,
)
def test_non_string_statements_are_rejected(value):
    """bool is an int subclass and bytes are sequence-like; both must fail."""
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        evaluator.validate_signoff_statement(value)
    assert excinfo.value.code == evaluator.SIGNOFF_STATEMENT_INVALID_TYPE


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "   ",
        "\t",
        "\t\t\t",
        "\n",
        "\n\n",
        "\r\n",
        " \t\n\r ",
        "\v\f",
        "\xa0",  # non-breaking space
        " ",  # narrow non-breaking space
        " ",  # figure space
        " ",  # em space
        "　",  # ideographic space
        " ",  # line separator
        " ",  # paragraph separator
        " ",  # ogham space mark
        " \xa0　\t ",  # mixed
        "​",  # zero-width space
        "﻿",  # zero-width no-break space
        "​‌‍⁠﻿",  # every zero-width code point
        " ​\t﻿\n",  # whitespace and zero-width together
        "​ ​ ​",  # alternating, so edge-stripping alone would not catch it
        "\t​\n‌ ⁠\r",  # alternating, mixed widths
    ],
    ids=repr,
)
def test_blank_statements_are_rejected(value):
    """Whitespace and zero-width code points assert nothing."""
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        evaluator.validate_signoff_statement(value)
    assert excinfo.value.code == evaluator.SIGNOFF_STATEMENT_EMPTY


def test_a_single_visible_character_is_accepted():
    """The bound is one character after trimming, and it is a real bound."""
    assert evaluator.validate_signoff_statement("x") == "x"


def test_a_normal_statement_is_accepted():
    text = (
        "I reviewed the nine scored pairs and the per-pair evidence, and I "
        "consider the extraction and comparison behaviour on this holdout "
        "adequately characterised."
    )
    assert evaluator.validate_signoff_statement(text) == text


def test_a_maximum_length_statement_is_accepted():
    text = "x" * evaluator.MAX_SIGNOFF_STATEMENT_CHARS
    assert evaluator.validate_signoff_statement(text) == text


def test_one_character_over_the_maximum_is_rejected():
    text = "x" * (evaluator.MAX_SIGNOFF_STATEMENT_CHARS + 1)
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        evaluator.validate_signoff_statement(text)
    assert excinfo.value.code == evaluator.SIGNOFF_STATEMENT_TOO_LONG


def test_the_maximum_matches_the_repository_bound_for_this_field():
    """Unchanged from the bound the field already carried."""
    assert evaluator.MAX_SIGNOFF_STATEMENT_CHARS == 2000


def test_internal_text_is_preserved_exactly():
    """A reviewer's sentence is evidence, not a field to reformat."""
    text = (
        "Reviewed  9 pairs (sic-2000s-01 … sic-5000s-02); coverage is 9/10 — "
        "the 10th is extraction-ambiguous, so it is NOT scored. Caveat: n=9."
    )
    assert evaluator.validate_signoff_statement(text) == text


def test_multi_line_statements_are_accepted_with_interior_newlines_intact():
    text = "Reviewed the scored pairs.\n\nCoverage is partial: 9 of 10.\nn=9."
    assert evaluator.validate_signoff_statement(text) == text


def test_leading_and_trailing_whitespace_is_trimmed_not_rejected():
    """The repository's convention for bounded human text, unchanged.

    ``rfb._require_bounded_str`` trims edges and returns the trimmed value, so
    this field does the same rather than inventing a second policy.
    """
    assert evaluator.validate_signoff_statement("  Reviewed.\n") == "Reviewed."
    assert evaluator.validate_signoff_statement("\t\tReviewed.\t") == "Reviewed."


def test_the_trimmed_statement_is_what_gets_persisted():
    block = _signoff(statement="  Reviewed the nine scored pairs.  ")
    validated = _validate(block)
    assert validated[evaluator.STATEMENT_FIELD] == "Reviewed the nine scored pairs."
    assert _claim(block)["signoff_statement"] == "Reviewed the nine scored pairs."


def test_validation_does_not_mutate_the_caller_document():
    block = _signoff(statement="  Reviewed.  ")
    document = {evaluator.SIGNOFF_FIELD: block}
    evaluator.validate_signoff_document(document)
    assert block[evaluator.STATEMENT_FIELD] == "  Reviewed.  "


def test_the_length_bound_counts_unicode_code_points():
    """Not bytes: a non-ASCII statement is not penalised for its encoding."""
    text = "é" * evaluator.MAX_SIGNOFF_STATEMENT_CHARS
    assert evaluator.validate_signoff_statement(text) == text


# --- The generalization gate --------------------------------------------------


def test_every_other_condition_met_but_no_statement_fails_admission():
    """Test 33: identity, timestamp, manifest, and coverage are not enough."""
    block = _signoff()
    del block[evaluator.STATEMENT_FIELD]
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        _claim(block)
    assert excinfo.value.code == evaluator.SIGNOFF_STATEMENT_REQUIRED


def test_every_other_condition_met_but_a_blank_statement_fails_admission():
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        _claim(_signoff(statement="   \n\t "))
    assert excinfo.value.code == evaluator.SIGNOFF_STATEMENT_EMPTY


def test_a_valid_statement_does_not_rescue_an_incomplete_evaluation():
    """Test 35: a run that scored no holdout cannot be signed into one."""
    claim = _claim(_signoff(), holdout_evaluation_performed=False)
    assert claim["supported"] is False
    assert any("did not perform a holdout evaluation" in r for r in claim["blocked_by"])


def test_a_valid_statement_does_not_rescue_a_different_manifest():
    claim = _claim(_signoff(), manifest_sha256="c" * 64)
    assert claim["supported"] is False
    assert any("different manifest" in r for r in claim["blocked_by"])


def test_a_valid_statement_does_not_rescue_unacknowledged_coverage():
    claim = _claim(_signoff(), pairs_scored=8)
    assert claim["supported"] is False
    assert any("did not see this coverage" in r for r in claim["blocked_by"])


@pytest.mark.parametrize("field", ["signer_id", "signed_at_utc", "manifest_sha256"])
def test_a_valid_statement_does_not_substitute_for_a_required_field(field):
    """Tests 37 and 38: the statement is additive, never a replacement."""
    block = _signoff()
    del block[field]
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        _claim(block)
    assert excinfo.value.code == "generalization_signoff_missing_keys"


def test_a_valid_statement_does_not_excuse_a_placeholder_signer():
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        _claim(_signoff(signer_id="TODO"))
    assert excinfo.value.code == "generalization_signoff_unattributed"


def test_a_valid_statement_does_not_excuse_a_naive_timestamp():
    """Test 39: the UTC requirement predates this change and still holds."""
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        _claim(_signoff(signed_at_utc="2026-03-01T09:30:00"))
    assert excinfo.value.code == "generalization_signoff_naive_timestamp"


def test_a_valid_statement_does_not_excuse_a_malformed_timestamp():
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        _claim(_signoff(signed_at_utc="March 2026"))
    assert excinfo.value.code == "generalization_signoff_invalid_timestamp"


def test_the_full_strict_path_permits_the_claim_in_a_synthetic_fixture():
    """Test 40: every existing condition, plus a statement, and only then."""
    claim = _claim(_signoff(statement="Reviewed the nine scored pairs. n=9."))
    assert claim["supported"] is True
    assert claim["blocked_by"] == []
    assert claim["signoff_present"] is True
    assert claim["signoff_statement"] == "Reviewed the nine scored pairs. n=9."


def test_no_default_statement_is_generated_anywhere():
    """Test 41: the validator has no fallback, and no caller supplies one.

    Checked at the source: no assignment or default in the evaluator binds the
    statement field to a literal, and the only literal statements in the tree
    are inside tests.
    """
    with pytest.raises(rfb.BenchmarkError):
        evaluator.validate_signoff_statement()

    source = (REPO_ROOT / "scripts" / "eval_real_filing_benchmark.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    for node in ast.walk(tree):
        # def validate_signoff_statement(value=<literal>) would be a default.
        if isinstance(node, ast.FunctionDef) and node.name == (
            "validate_signoff_statement"
        ):
            for default in node.args.defaults:
                assert not isinstance(default, ast.Constant), (
                    "the statement parameter must not default to a literal"
                )


def test_the_annotator_identity_is_never_reused_as_the_signer():
    """Test 42: a person who labelled pairs did not thereby sign a claim.

    The two roles live in disjoint schemas. An annotation records who verified
    LABELS; a sign-off records who asserted a CLAIM. No field is shared, so
    there is nothing for one to be silently read as the other.
    """
    annotation_fields = set(rfb._ANNOTATION_REQUIRED) | set(rfb._ANNOTATION_OPTIONAL)
    signoff_fields = set(evaluator._SIGNOFF_REQUIRED) | {evaluator.STATEMENT_FIELD}
    assert annotation_fields & signoff_fields == set()
    assert "annotator_id" in annotation_fields
    assert "annotator_id" not in signoff_fields
    assert "signer_id" in signoff_fields
    assert "signer_id" not in annotation_fields


def test_gold_evaluator_completion_does_not_sign_off(committed_gold_report):
    """Test 43: the committed report is the proof — it completed, unsigned."""
    assert committed_gold_report["gold_metrics_available"] is True
    assert committed_gold_report["generalization_claim"]["signoff_present"] is False
    assert committed_gold_report["generalization_claim_supported"] is False


def test_metric_values_play_no_part_in_the_claim(committed_gold_report):
    """Test 44: no metric, at any value, can synthesize or block a sign-off.

    The committed report spans the range — one rate at its best possible value,
    others mid-range, two with zero denominators — and the claim is false for
    the same single reason throughout: nobody signed. The gate is structurally
    incapable of reading a metric, because none is passed to it.
    """
    metrics = committed_gold_report["gold_metrics"]
    assert metrics["evidence_resolution_rate"]["value"] == 1.0
    assert 0.0 < metrics["change_precision"]["value"] < 1.0
    assert metrics["undetermined_reason_accuracy"]["value"] is None
    assert committed_gold_report["generalization_claim_supported"] is False

    import inspect

    parameters = set(
        inspect.signature(evaluator.evaluate_generalization_claim).parameters
    )
    assert parameters == {
        "config_document",
        "manifest_sha256",
        "holdout_evaluation_performed",
        "pairs_scored",
        "coverage_complete",
        "pairs_in_manifest",
    }


def test_full_coverage_does_not_synthesize_a_signoff():
    """Coverage travels with the claim as context; it never grants it."""
    claim = _claim(None, coverage_complete=True, pairs_scored=10, pairs_in_manifest=10)
    assert claim["coverage_complete"] is True
    assert claim["supported"] is False


def test_the_claim_cannot_become_true_through_legacy_parsing():
    """Test 45: no legacy shape reaches an affirmative claim."""
    for legacy in ({}, {evaluator.SIGNOFF_FIELD: None}):
        claim = evaluator.evaluate_generalization_claim(
            legacy,
            manifest_sha256="b" * 64,
            holdout_evaluation_performed=True,
            pairs_scored=9,
            coverage_complete=True,
            pairs_in_manifest=10,
        )
        assert claim["supported"] is False
        assert claim["signoff_statement"] is None


# --- Failure and safety -------------------------------------------------------


def test_an_invalid_statement_writes_no_artifact(tmp_path, capsys):
    """The CLI refuses before it computes or writes anything."""
    corpus_dir = tmp_path / "corpus"
    report_path = tmp_path / "report.json"
    config_path = fx.write_holdout_evaluation_config(
        tmp_path, signoff=_signoff(statement="   ")
    )
    before = sorted(p.name for p in tmp_path.iterdir())

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = evaluator.main(
            [
                "--manifest",
                str(HOLDOUT_MANIFEST),
                "--corpus-dir",
                str(corpus_dir),
                "--evaluation-config",
                str(config_path),
                "--report",
                str(report_path),
            ]
        )

    assert code == 2
    assert not report_path.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert evaluator.SIGNOFF_STATEMENT_EMPTY in capsys.readouterr().err


def test_an_invalid_statement_does_not_modify_its_input_file(tmp_path):
    config_path = fx.write_holdout_evaluation_config(
        tmp_path, signoff=_signoff(statement="")
    )
    digest = _sha256_file(config_path)
    with pytest.raises(rfb.BenchmarkError):
        evaluator.validate_signoff_document(
            json.loads(config_path.read_text(encoding="utf-8"))
        )
    assert _sha256_file(config_path) == digest


def test_the_cli_exit_code_is_stable_across_every_rejection(tmp_path, capsys):
    """Stable nonzero exit, one code, for every invalid statement shape."""
    for statement, expected in (
        ("", evaluator.SIGNOFF_STATEMENT_EMPTY),
        ("   ", evaluator.SIGNOFF_STATEMENT_EMPTY),
        ("​", evaluator.SIGNOFF_STATEMENT_EMPTY),
        (42, evaluator.SIGNOFF_STATEMENT_INVALID_TYPE),
        (None, evaluator.SIGNOFF_STATEMENT_INVALID_TYPE),
        ("x" * 2001, evaluator.SIGNOFF_STATEMENT_TOO_LONG),
    ):
        config_path = fx.write_holdout_evaluation_config(
            tmp_path, signoff=_signoff(statement=statement)
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = evaluator.main(
                ["--manifest", str(HOLDOUT_MANIFEST), "--evaluation-config",
                 str(config_path)]
            )
        assert code == 2, statement
        assert expected in capsys.readouterr().err, statement


def test_validation_is_deterministic():
    text = "Reviewed the nine scored pairs."
    results = {evaluator.validate_signoff_statement(text) for _ in range(50)}
    assert results == {text}

    codes = set()
    for _ in range(50):
        with pytest.raises(rfb.BenchmarkError) as excinfo:
            evaluator.validate_signoff_statement("  ")
        codes.add(excinfo.value.code)
    assert codes == {evaluator.SIGNOFF_STATEMENT_EMPTY}


def test_error_ordering_is_deterministic():
    """present, then type, then emptiness, then length — always in that order."""
    block = _signoff()
    del block[evaluator.STATEMENT_FIELD]
    for _ in range(5):
        with pytest.raises(rfb.BenchmarkError) as excinfo:
            _validate(block)
        assert excinfo.value.code == evaluator.SIGNOFF_STATEMENT_REQUIRED

    # A value that is both blank and overlong reports blank, every time.
    for _ in range(5):
        with pytest.raises(rfb.BenchmarkError) as excinfo:
            evaluator.validate_signoff_statement(" " * 5000)
        assert excinfo.value.code == evaluator.SIGNOFF_STATEMENT_EMPTY


def test_error_codes_are_stable_and_namespaced():
    codes = (
        evaluator.SIGNOFF_STATEMENT_REQUIRED,
        evaluator.SIGNOFF_STATEMENT_INVALID_TYPE,
        evaluator.SIGNOFF_STATEMENT_EMPTY,
        evaluator.SIGNOFF_STATEMENT_TOO_LONG,
    )
    assert codes == (
        "generalization_signoff_statement_required",
        "generalization_signoff_statement_invalid_type",
        "generalization_signoff_statement_empty",
        "generalization_signoff_statement_too_long",
    )
    assert len(set(codes)) == len(codes)


@pytest.mark.parametrize(
    "value",
    [
        "SECRET-TOKEN-abc123 " * 200,  # over the bound, and carries a secret
        "​",
        42,
        {"labels": ["material_change"]},
        ["/Users/someone/benchmark_data/AWS_SECRET_ACCESS_KEY"],
    ],
    ids=lambda v: repr(v)[:40],
)
def test_errors_leak_nothing_from_the_rejected_value(value):
    """No report body, no annotation label, no path, no environment value, no
    raw exception text — the message names the field and the rule only."""
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        evaluator.validate_signoff_statement(value)
    message = excinfo.value.message
    assert "SECRET" not in message
    assert "material_change" not in message
    assert str(REPO_ROOT) not in message
    assert "/" not in message
    assert "Traceback" not in message
    assert len(message) < 300
    assert evaluator.SIGNOFF_FIELD in message


def test_the_validator_needs_no_network_no_credentials_and_no_parser():
    """Checked at the import graph, the way the holdout modules are checked."""
    source = (REPO_ROOT / "scripts" / "eval_real_filing_benchmark.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in (
        "urllib",
        "http",
        "socket",
        "requests",
        "boto3",
        "botocore",
        "real_filing_acquisition",
        "real_filing_holdout_acquisition",
    ):
        assert forbidden not in imported, forbidden

    # The validator itself touches no module state at all.
    assert evaluator.validate_signoff_statement("Reviewed.") == "Reviewed."


def test_no_signoff_artifact_exists_in_the_repository():
    """No signed report, no signoff file, was created by this work."""
    for path in HOLDOUT_DIR.iterdir():
        assert "signoff" not in path.name, path.name
    document = json.loads(HOLDOUT_CONFIG.read_text(encoding="utf-8"))
    assert document[evaluator.SIGNOFF_FIELD] is None


def test_no_tool_writes_a_signoff_statement():
    """Same contract as ``human_verified``: no writer may exist.

    A statement some script could stamp would not be a human judgement. Only
    the evaluator's validator and test fixtures may name the field.
    """
    allowed = {
        Path("scripts/eval_real_filing_benchmark.py"),
        Path("tests/helpers/real_filing_fixtures.py"),
        Path("tests/test_real_filing_holdout_gold_evaluation.py"),
        Path(THIS_SUITE),
    }
    offenders = []
    for path in REPO_ROOT.rglob("*.py"):
        relative = path.relative_to(REPO_ROOT)
        if relative in allowed or ".venv" in relative.parts:
            continue
        if "signoff_statement" in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(str(relative))
    assert offenders == [], offenders


# --- CI wiring ----------------------------------------------------------------


def _workflow():
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def test_this_suite_runs_in_the_required_check():
    """A suite the required check does not run cannot block a regression."""
    job = _workflow()["jobs"]["comparison-regression"]
    runs = "\n".join(
        step["run"]
        for step in job["steps"]
        if isinstance(step.get("run"), str) and "pytest" in step["run"]
    )
    assert THIS_SUITE in runs
    assert (REPO_ROOT / THIS_SUITE).is_file()


def test_required_check_identity_is_unchanged():
    """Adding this suite must not disturb branch protection."""
    workflow = _workflow()
    assert workflow["name"] == "comparison-regression"
    assert list(workflow["jobs"]) == ["comparison-regression"]
    job = workflow["jobs"]["comparison-regression"]
    assert job["name"] == "comparison-regression"
    triggers = workflow.get("on", workflow.get(True))
    assert "pull_request" in triggers
    assert triggers["push"]["branches"] == ["main"]
    for value in triggers.values():
        if isinstance(value, dict):
            assert "paths" not in value
            assert "paths-ignore" not in value


def test_required_check_introduces_no_credentials_or_network():
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "--allow-network",
        "SEC_USER_AGENT",
        "sec.gov",
        "secrets.",
        "AWS_ACCESS_KEY",
        "configure-aws-credentials",
    ):
        assert forbidden not in raw, forbidden


def test_required_check_retains_the_regression_evaluator_and_artifact():
    job = _workflow()["jobs"]["comparison-regression"]
    steps = json.dumps(job["steps"])
    assert "scripts/eval_comparison_regression.py" in steps
    assert "actions/upload-artifact" in steps
    assert "comparison-regression-report.json" in steps


# --- Documentation truth ------------------------------------------------------
#
# README.MD and BENCHMARK.md describe the CURRENT state of the benchmark. When
# the holdout was annotated and evaluated, several of their passages became
# false while remaining literally quotable, which is the worst failure mode a
# status document has. These tests derive what the documents must say from the
# committed report itself, so the documents cannot drift back.
#
# The scans are sentence-scoped on purpose. "generalization is not supported"
# is an honest negation and must stay legal; "no gold evaluation has run" is a
# stale current-state claim and must not. The difference is the scope marker in
# the same sentence, not the words in isolation.

#: Wording that was true before the holdout was annotated and evaluated, and is
#: false now unless the sentence says which corpus or which commit it describes.
STALE_CURRENT_STATE = (
    "no gold evaluation has run",
    "no gold evaluation exists",
    "gold evaluation has not",
    "no real-filing evaluation",
    "no real-filing accuracy claim exists",
    "no accuracy number exists",
    "no accuracy number of any kind",
    "zero labels are `human_verified`",
    "zero holdout labels",
    "no label is `human_verified`",
    "not been evaluated",
)

#: A sentence carrying one of the above is honest only if it also says which
#: corpus or which point in history it is talking about.
SCOPE_MARKERS = (
    "development",
    "at that commit",
    "at the commit",
    "blind-extraction commit",
    "blind-run",
    "*this* corpus",
    "this development corpus",
    "for it",
)


def _sentences(text: str) -> list[str]:
    """Split loosely on sentence and list-item boundaries.

    Deliberately crude: over-splitting only makes the scope test stricter,
    because a marker then has to sit closer to the claim it qualifies.
    """
    parts = re.split(r"(?<=[.!?])\s+|\n\s*[-*|]\s*|\n\n+", text)
    return [p.strip() for p in parts if p.strip()]


def _doc_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_markup(text: str) -> str:
    """Drop markdown emphasis and code ticks, keeping identifiers intact.

    "**not** risk-factor-item-level accuracy" and "not risk-factor-item-level
    accuracy" are the same claim; only the rendering differs. Underscores are
    NOT stripped — doing so would turn `pairs_scored` into `pairsscored` and
    make every identifier assertion unsearchable.
    """
    return re.sub(r"[*`]+", "", text).lower()


def _plain(path: Path) -> str:
    return _strip_markup(_doc_text(path))


#: Words that turn a banned phrase into an honest denial. "not a statistically
#: representative sample" and "would misstate ... as per-risk-factor accuracy"
#: are the correct way to say these things, so the scan is sentence-scoped and
#: rejects the affirmative use only.
DENIAL_MARKERS = ("not", "never", "cannot", "no ", "false", "unsigned", "misstate")


def _affirmative_hits(path: Path, phrases: tuple[str, ...]) -> list[str]:
    """Sentences that assert a banned phrase without denying it."""
    hits = []
    for sentence in _sentences(_strip_markup(_doc_text(path))):
        if any(marker in sentence for marker in DENIAL_MARKERS):
            continue
        for phrase in phrases:
            if phrase in sentence:
                hits.append(f"{phrase!r} in: {sentence[:160]}")
    return hits


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_docs_state_that_a_frozen_gold_evaluation_exists(path):
    """Tests 1 and 2: the documents must say the evaluation happened."""
    text = _doc_text(path).lower()
    assert "gold_evaluation_report.json" in text
    assert "gold-evaluated" in text or "gold evaluation" in text
    assert "human-annotated" in text or "human_verified" in text


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_docs_name_the_report_as_the_metric_source_of_truth(path):
    """Test 3: one authoritative copy of the numbers, and both docs point at it.

    Same sentence, not merely the same file: a document that names the report
    somewhere and says "source of truth" about something else has not told the
    reader where the numbers live.
    """
    report_path = "benchmarks/real_filing_holdout_v1/gold_evaluation_report.json"
    assert report_path in _doc_text(path)
    naming = [
        sentence
        for sentence in _sentences(_plain(path))
        if report_path in sentence and "source of truth" in sentence
    ]
    assert naming, f"{path.name} never calls the report the metric source of truth"


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_docs_link_the_detailed_writeup(path):
    """Test 4: the narrative and error analysis live in one place."""
    assert "HOLDOUT_EVALUATION.md" in _doc_text(path)


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_docs_distinguish_human_verification_from_signoff(path):
    """Test 4/13: verified labels are not a generalization sign-off."""
    text = _doc_text(path).lower()
    assert "sign-off" in text or "signoff" in text
    assert "not" in text and (
        "not themselves a generalization sign-off" in text
        or "are not a sign-off" in text
        or "not a generalization sign-off" in text
    )


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_docs_state_the_claim_is_false_and_say_why(path, committed_gold_report):
    """Test 5: matches the report, and attributes it to the missing sign-off."""
    assert committed_gold_report["generalization_claim_supported"] is False
    text = _doc_text(path)
    assert "`generalization_claim_supported` is `false`" in text.lower() or (
        "generalization_claim_supported = false" in text.lower()
        or "generalization_claim_supported` remains `false`" in text.lower()
        or "generalization_claim_supported is still false" in text.lower()
    )
    lowered = text.lower()
    assert "no sign-off exists" in lowered or "no human has signed" in lowered


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_docs_carry_no_unscoped_stale_current_state_claim(path):
    """Tests 6-9: stale wording survives only when explicitly scoped."""
    offenders = []
    for sentence in _sentences(_doc_text(path)):
        lowered = sentence.lower()
        for phrase in STALE_CURRENT_STATE:
            if phrase in lowered and not any(
                marker.lower() in lowered for marker in SCOPE_MARKERS
            ):
                offenders.append(f"{phrase!r} in: {sentence[:160]}")
    assert offenders == [], offenders


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_docs_never_call_the_metrics_risk_factor_item_level_accuracy(path):
    """Test 10: the frozen segmentation often did not operate at that level."""
    claims = (
        "risk-factor-level accuracy",
        "per-risk-factor accuracy",
        "risk-factor-item accuracy",
        "validated risk-item extraction",
    )
    offenders = _affirmative_hits(path, claims)
    assert offenders == [], offenders
    # The denial itself must be present.
    assert "not risk-factor-item-level accuracy" in _plain(path)


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_docs_preserve_the_unit_granularity_limitation(path):
    """Test 11: what was actually measured, stated where the metrics are cited."""
    lowered = _doc_text(path).lower()
    assert "unit granularity" in lowered or "frozen v2 unit granularity" in lowered
    assert "segmentation" in lowered


def test_benchmark_doc_records_the_denominator_boundary(committed_gold_report):
    """Test 7 of the required state: the blocked pair is not silently counted."""
    scope = committed_gold_report["scoring_scope"]
    excluded = scope["excluded_pairs"][0]
    text = _doc_text(BENCHMARK_DOC)
    assert excluded["pair_id"] in text
    assert excluded["code"] in text
    assert "never silently" in text.lower() or "explicit exclusion" in text.lower()


def test_benchmark_doc_counts_match_the_committed_report(committed_gold_report):
    """Test 15: every count BENCHMARK.md states is read back from the report."""
    scope = committed_gold_report["scoring_scope"]
    stats = committed_gold_report["label_statistics"]
    text = _plain(BENCHMARK_DOC)
    assert f"pairs_scored: {scope['pairs_scored']}" in text
    assert f"pairs_in_manifest: {scope['pairs_in_manifest']}" in text
    assert f"coverage_complete: {str(scope['coverage_complete']).lower()}" in text
    assert (
        f"{scope['pairs_scored']} of the {scope['pairs_in_manifest']} frozen pairs"
        in text
    )
    assert f"{stats['human_verified_label_count']} labels are human_verified" in text
    assert committed_gold_report["detector_version"] in text
    assert committed_gold_report["workflow_version"] in text
    assert "manifest_status" in text
    assert committed_gold_report["manifest_status"] in text


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_docs_do_not_duplicate_the_metric_table(path, committed_gold_report):
    """One authoritative copy of the numbers, so no two can disagree.

    The report is the machine-readable source and HOLDOUT_EVALUATION.md is the
    narrative. Restating the decimal values in a status document creates a
    third copy that drifts silently, so none of them appears here.
    """
    text = _doc_text(path)
    for name, metric in committed_gold_report["gold_metrics"].items():
        value = metric["value"]
        # 1.0 and null are not distinctive enough to search for; the four
        # multi-decimal rates are, and they are the ones that could drift.
        if value is None or len(str(value)) < 6:
            continue
        assert str(value) not in text, f"{name} value restated in {path.name}"
        fraction = f"{metric['numerator']}/{metric['denominator']}"
        assert fraction not in text, f"{name} fraction restated in {path.name}"


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_docs_claim_no_completion_readiness_or_generalization(path):
    """Tests 12-14, checked sentence-scoped so denials stay legal."""
    banned = (
        "stage 3.5 complete",
        "stage 3.5 is complete",
        "production-ready",
        "production ready",
        "enterprise-ready",
        "compliance-grade",
        "statistically generalizable",
        "generalization is supported",
        "generalization claim is supported",
        "representative sample",
    )
    # Sentence-scoped, so an explicit denial stays legal: "not a statistically
    # representative sample" is the honest form of "representative sample".
    offenders = _affirmative_hits(path, banned)
    assert offenders == [], offenders


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_docs_state_the_stage_assessment(path):
    """Stage 3 current, Stage 3.5 in progress — unchanged by this evaluation."""
    lowered = _doc_text(path).lower()
    assert "stage 3.5 remains in progress" in lowered
    assert "stage 3" in lowered


def test_benchmark_doc_requires_a_new_holdout_for_parser_v3():
    """Test 16 of the required state: observed failures cannot be tuned away."""
    lowered = _doc_text(BENCHMARK_DOC).lower()
    assert "parser v3" in lowered
    assert "newly frozen unseen holdout" in lowered or "new unseen holdout" in lowered
    assert "already been observed" in lowered


def test_benchmark_doc_preserves_the_metadata_replayability_limitation():
    """Selection is auditable in outcome, not replayable in process."""
    lowered = _doc_text(BENCHMARK_DOC).lower()
    assert "not fully replayable" in lowered
    assert "byte-for-byte" in lowered


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_docs_do_not_claim_the_evaluation_is_absent(path):
    """The correction this suite exists to hold: it happened, and it is unsigned."""
    lowered = _doc_text(path).lower()
    assert "has since been" in lowered or "has since run" in lowered


def test_the_writeup_is_byte_identical():
    """Test 18: HOLDOUT_EVALUATION.md is not edited by documentation sync."""
    assert _sha256_file(WRITEUP) == WRITEUP_SHA256


def test_documentation_tests_need_no_evaluator_execution():
    """Tests 19-20: reading committed files only — no run, no network, no AWS.

    Asserted structurally: this module never invokes the gold-report builders,
    and the committed report's own timestamp proves it was not regenerated.
    """
    source = (REPO_ROOT / THIS_SUITE).read_text(encoding="utf-8")
    called = {
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for forbidden in ("gold_report", "unlabeled_report", "collect_pair_state"):
        assert forbidden not in called, forbidden
    # The committed report carries its own evaluation timestamp; nothing here
    # recomputes it, and its byte identity is pinned above.
    assert json.loads(GOLD_REPORT.read_text(encoding="utf-8"))["evaluated_at"]
