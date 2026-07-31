"""Evaluator tests: refusals, gold metrics, denominators, and report hygiene.

Offline: synthetic HTML filings in temporary directories, the real ingestion
and comparison paths, no network, no AWS, no LLM.

The refusal tests carry most of the weight. An evaluator that reports numbers
when its inputs are unverified is worse than one that reports nothing, because
the numbers get quoted.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

import comparison_detector
import comparison_store
import real_filing_benchmark as rfb
from scripts import build_real_filing_benchmark as builder
from scripts import create_real_filing_annotation_packets as packets
from scripts import eval_real_filing_benchmark as evaluator
from tests.helpers import real_filing_fixtures as fx

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "comparison-regression.yml"

BENCHMARK_SUITES = (
    "tests/test_real_filing_benchmark_schema.py",
    "tests/test_real_filing_benchmark_tools.py",
    "tests/test_real_filing_benchmark_evaluator.py",
)


@pytest.fixture(scope="module")
def evaluation_config():
    return evaluator.load_evaluation_config()


@pytest.fixture
def corpus(tmp_path):
    """A built single-pair corpus plus its machine-proposed annotation."""
    document = fx.single_pair_manifest()
    manifest_path = fx.write_manifest(tmp_path, document)
    layout = rfb.CorpusLayout(tmp_path / "benchmark_data")
    fx.seed_sources(
        layout,
        document,
        {
            ("pair-01", "previous"): fx.PREVIOUS_HTML,
            ("pair-01", "current"): fx.CURRENT_HTML,
        },
    )
    record = builder.build_pair(document["pairs"][0], document, layout)
    _packet, annotation = packets.build_packet("pair-01", layout, document)
    return {
        "manifest": document,
        "manifest_path": manifest_path,
        "layout": layout,
        "record": record,
        "machine_annotation": annotation,
    }


def _states(corpus):
    return [
        evaluator.collect_pair_state(pair, corpus["layout"])
        for pair in rfb.manifest_pairs(corpus["manifest"])
    ]


def _report(corpus, evaluation_config, **kwargs):
    return evaluator.gold_report(
        corpus["manifest"],
        corpus["manifest_path"],
        rfb.manifest_pairs(corpus["manifest"]),
        _states(corpus),
        corpus["layout"],
        evaluation_config,
        **kwargs,
    )


def _verify(corpus, annotation=None, **kwargs):
    verified = fx.human_verify(annotation or corpus["machine_annotation"], **kwargs)
    rfb.write_json_atomic(corpus["layout"].annotation_path("pair-01"), verified)
    return verified


# --- Refusals -----------------------------------------------------------------


def test_gold_evaluation_refuses_without_any_annotation(corpus, evaluation_config):
    report = _report(corpus, evaluation_config)
    assert report["refused"] is True
    assert report["gold_metrics_available"] is False
    assert report["gold_metrics"] is None
    assert any(
        reason["code"] == "annotation_not_found"
        for reason in report["refusal_reasons"]
    )


def test_gold_evaluation_refuses_a_machine_proposed_annotation(
    corpus, evaluation_config
):
    rfb.write_json_atomic(
        corpus["layout"].annotation_path("pair-01"), corpus["machine_annotation"]
    )
    report = _report(corpus, evaluation_config)
    assert report["refused"] is True
    reasons = {reason["code"] for reason in report["refusal_reasons"]}
    assert "pair_not_human_verified" in reasons
    detail = next(
        reason["detail"]
        for reason in report["refusal_reasons"]
        if reason["code"] == "pair_not_human_verified"
    )
    assert "machine proposal is not a verified label" in detail


def test_gold_evaluation_refuses_on_section_hash_drift(corpus, evaluation_config):
    verified = fx.human_verify(corpus["machine_annotation"])
    verified["current_section_hash"] = "d" * 64
    rfb.write_json_atomic(corpus["layout"].annotation_path("pair-01"), verified)
    report = _report(corpus, evaluation_config)
    assert report["refused"] is True
    assert any(
        reason["code"] == "annotation_section_hash_drift"
        for reason in report["refusal_reasons"]
    )


def test_gold_evaluation_refuses_on_source_checksum_drift(corpus, evaluation_config):
    _verify(corpus)
    drifted = copy.deepcopy(corpus["manifest"])
    drifted["pairs"][0]["previous"]["expected_sha256"] = "e" * 64
    report = evaluator.gold_report(
        drifted,
        corpus["manifest_path"],
        rfb.manifest_pairs(drifted),
        _states(corpus),
        corpus["layout"],
        evaluation_config,
    )
    assert report["refused"] is True
    assert any(
        reason["code"] == "source_checksum_drift"
        for reason in report["refusal_reasons"]
    )


def test_gold_evaluation_refuses_unknown_unit_references(corpus, evaluation_config):
    verified = fx.human_verify(corpus["machine_annotation"])
    verified["labels"][0]["previous_unit_id"] = None
    verified["labels"][0]["current_unit_id"] = "current:099:not-a-real-unit"
    verified["labels"][0]["expected_change_type"] = "added"
    verified["labels"][0]["expected_evidence_side"] = "current"
    verified["labels"][0]["expected_reason_code"] = None
    rfb.write_json_atomic(corpus["layout"].annotation_path("pair-01"), verified)
    report = _report(corpus, evaluation_config)
    assert report["refused"] is True
    assert any(
        reason["code"] == "annotation_unknown_unit_reference"
        for reason in report["refusal_reasons"]
    )


def test_gold_evaluation_refuses_a_detector_version_mismatch(
    corpus, evaluation_config
):
    _verify(corpus)
    stale = dict(evaluation_config, declared_detector_version="item1a_detector.v1")
    report = _report(corpus, stale)
    assert report["refused"] is True
    assert any(
        reason["code"] == "detector_version_mismatch"
        for reason in report["refusal_reasons"]
    )


def test_gold_evaluation_refuses_a_workflow_version_mismatch(
    corpus, evaluation_config
):
    _verify(corpus)
    stale = dict(evaluation_config, declared_workflow_version="comparison_workflow.v1")
    report = _report(corpus, stale)
    assert report["refused"] is True
    assert any(
        reason["code"] == "workflow_version_mismatch"
        for reason in report["refusal_reasons"]
    )


def test_explicit_new_run_accepts_a_version_change(corpus, evaluation_config):
    _verify(corpus)
    stale = dict(evaluation_config, declared_detector_version="item1a_detector.v1")
    report = _report(corpus, stale, new_run=True)
    assert report["refused"] is False
    assert report["gold_metrics_available"] is True


def test_committed_evaluation_config_matches_the_live_versions(evaluation_config):
    assert (
        evaluation_config["declared_detector_version"]
        == comparison_detector.DETECTOR_VERSION
    )
    assert (
        evaluation_config["declared_workflow_version"]
        == comparison_store.WORKFLOW_VERSION
    )
    assert evaluation_config["pass_fail_thresholds"] is None
    assert evaluation_config["gold_status_required"] == rfb.GOLD_STATUS


def test_refusal_collects_every_reason_not_just_the_first(corpus, evaluation_config):
    verified = fx.human_verify(corpus["machine_annotation"])
    verified["previous_section_hash"] = "d" * 64
    rfb.write_json_atomic(corpus["layout"].annotation_path("pair-01"), verified)
    stale = dict(evaluation_config, declared_detector_version="item1a_detector.v1")
    report = _report(corpus, stale)
    codes = {reason["code"] for reason in report["refusal_reasons"]}
    assert "detector_version_mismatch" in codes
    assert "annotation_section_hash_drift" in codes


# --- Gold metrics -------------------------------------------------------------


def test_gold_metrics_include_only_human_verified_pairs(corpus, evaluation_config):
    _verify(corpus)
    report = _report(corpus, evaluation_config)
    assert report["refused"] is False
    assert report["corpus_quality"]["pairs_human_verified"] == 1
    assert report["corpus_quality"]["pairs_machine_proposed_only"] == 0
    assert [score["pair_id"] for score in report["per_pair"]] == ["pair-01"]
    for score in report["per_pair"]:
        assert score["annotation_status"] == rfb.ANNOTATION_HUMAN_VERIFIED


def test_perfect_agreement_scores_one_across_the_change_metrics(
    corpus, evaluation_config
):
    """The machine proposal, verified verbatim, agrees with the detector by
    construction — so this pins the metric plumbing, not detector quality."""
    _verify(corpus)
    report = _report(corpus, evaluation_config)
    metrics = report["gold_metrics"]
    assert metrics["change_precision"]["value"] == 1.0
    assert metrics["change_recall"]["value"] == 1.0
    assert metrics["change_type_accuracy"]["value"] == 1.0
    assert metrics["unchanged_false_positive_rate"]["value"] == 0.0
    assert metrics["pair_exact_match_rate"]["value"] == 1.0
    assert report["per_pair"][0]["exact_match"] is True


def test_a_wrong_label_lowers_precision_and_recall(corpus, evaluation_config):
    """A disagreeing human label must move the metrics; if it cannot, the
    metrics are not measuring anything."""
    annotation = copy.deepcopy(corpus["machine_annotation"])
    modified = next(
        label
        for label in annotation["labels"]
        if label["expected_change_type"] == "modified"
    )
    modified["expected_change_type"] = "unchanged"
    _verify(corpus, annotation)
    report = _report(corpus, evaluation_config)
    metrics = report["gold_metrics"]
    assert metrics["change_precision"]["value"] < 1.0
    assert metrics["unchanged_false_positive_rate"]["value"] > 0.0
    assert report["per_pair"][0]["exact_match"] is False


def test_every_rate_reports_its_numerator_and_denominator(corpus, evaluation_config):
    _verify(corpus)
    report = _report(corpus, evaluation_config)
    for name, metric in report["gold_metrics"].items():
        assert metric["metric"] == name
        assert isinstance(metric["numerator"], int)
        assert isinstance(metric["denominator"], int)
        assert "zero_denominator" in metric
    for score in report["per_pair"]:
        for metric in score["metrics"].values():
            assert isinstance(metric["denominator"], int)


def test_zero_denominator_metrics_are_null_not_zero(corpus, evaluation_config):
    """No label carries a direction, so its denominator is zero — and a zero
    denominator asserts nothing, so it must not read as 0.0."""
    _verify(corpus)
    report = _report(corpus, evaluation_config)
    direction = report["gold_metrics"]["direction_consistency_accuracy"]
    assert direction["denominator"] == 0
    assert direction["value"] is None
    assert direction["zero_denominator"] is True
    assert direction["zero_denominator_policy"] == rfb.ZERO_DENOMINATOR_POLICY


def test_direction_metric_activates_only_when_a_direction_is_labelled(
    corpus, evaluation_config
):
    annotation = copy.deepcopy(corpus["machine_annotation"])
    modified = next(
        label
        for label in annotation["labels"]
        if label["expected_change_type"] == "modified"
    )
    modified["expected_direction"] = "increased"
    _verify(corpus, annotation)
    report = _report(corpus, evaluation_config)
    direction = report["gold_metrics"]["direction_consistency_accuracy"]
    assert direction["denominator"] == 1
    assert direction["value"] in (0.0, 1.0)


def test_undetermined_reason_denominator_counts_only_matching_undetermined(
    corpus, evaluation_config
):
    _verify(corpus)
    report = _report(corpus, evaluation_config)
    reason = report["gold_metrics"]["undetermined_reason_accuracy"]
    assert reason["denominator"] == report["per_pair"][0]["undetermined_reason_total"]


def test_metric_notes_document_the_divergent_denominators(corpus, evaluation_config):
    _verify(corpus)
    report = _report(corpus, evaluation_config)
    notes = report["metric_notes"]
    # Every reported gold metric carries a definition note; a metric nobody can
    # look up the denominator for is a metric that will be misread.
    assert set(notes) == set(report["gold_metrics"])
    assert "DEFINITION DIFFERS" in notes["direction_consistency_accuracy"]
    assert "DENOMINATOR DIFFERS" in notes["undetermined_reason_accuracy"]
    assert "Same definition as the synthetic" in notes["change_precision"]


def test_no_pass_fail_threshold_exists(corpus, evaluation_config):
    _verify(corpus)
    report = _report(corpus, evaluation_config)
    assert report["pass_fail_thresholds"] is None
    assert "merge-blocking deterministic gate" in report["threshold_policy"]
    assert "gates" not in report


def test_label_statistics_report_confidence_without_reviewer_notes(
    corpus, evaluation_config
):
    annotation = copy.deepcopy(corpus["machine_annotation"])
    annotation["labels"][0]["reviewer_note"] = "Checked the FY23 insurance figure."
    annotation["labels"][0]["confidence"] = "high"
    _verify(corpus, annotation)
    report = _report(corpus, evaluation_config)
    stats = report["label_statistics"]
    assert stats["human_verified_label_count"] == len(annotation["labels"])
    assert stats["confidence_distribution"]["high"] >= 1
    assert stats["reviewer_notes_included"] is False
    assert "Checked the FY23 insurance figure." not in json.dumps(report)


# --- Provenance and corpus quality --------------------------------------------


def test_report_carries_full_provenance(corpus, evaluation_config):
    _verify(corpus)
    report = _report(corpus, evaluation_config)
    assert report["detector_version"] == comparison_detector.DETECTOR_VERSION
    assert report["workflow_version"] == comparison_store.WORKFLOW_VERSION
    assert len(report["manifest_hash"]) == 64
    assert len(report["annotation_hash"]) == 64
    assert report["metric_definitions_version"] == rfb.METRIC_DEFINITIONS_VERSION
    assert report["annotation_protocol_version"] == rfb.ANNOTATION_PROTOCOL_VERSION
    assert report["evaluated_at"]
    # commit_sha is None outside a git checkout rather than invented.
    assert report["commit_sha"] is None or len(report["commit_sha"]) == 40


def test_corpus_quality_counts_each_stage(corpus, evaluation_config):
    _verify(corpus)
    quality = _report(corpus, evaluation_config)["corpus_quality"]
    assert quality["pairs_requested"] == 1
    assert quality["pairs_source_verified"] == 1
    assert quality["pairs_built"] == 1
    assert quality["pairs_extracted"] == 1
    assert quality["pairs_missing_section"] == 0
    assert quality["pairs_ambiguous_section"] == 0
    assert quality["pairs_parse_failed"] == 0
    assert quality["pairs_human_verified"] == 1


def test_corpus_quality_counts_a_missing_section_pair(tmp_path, evaluation_config):
    document = fx.single_pair_manifest(current_html=fx.NO_SECTION_HTML)
    manifest_path = fx.write_manifest(tmp_path, document)
    layout = rfb.CorpusLayout(tmp_path / "benchmark_data")
    fx.seed_sources(
        layout,
        document,
        {
            ("pair-01", "previous"): fx.PREVIOUS_HTML,
            ("pair-01", "current"): fx.NO_SECTION_HTML,
        },
    )
    builder.build_pair(document["pairs"][0], document, layout)
    states = [
        evaluator.collect_pair_state(pair, layout)
        for pair in rfb.manifest_pairs(document)
    ]
    report = evaluator.unlabeled_report(
        document,
        manifest_path,
        rfb.manifest_pairs(document),
        states,
        layout,
        evaluation_config,
    )
    quality = report["corpus_quality"]
    assert quality["pairs_missing_section"] == 1
    assert quality["pairs_extracted"] == 0
    assert quality["pairs_human_verified"] == 0


# --- Unlabeled execution report ----------------------------------------------


def _unlabeled(corpus, evaluation_config):
    return evaluator.unlabeled_report(
        corpus["manifest"],
        corpus["manifest_path"],
        rfb.manifest_pairs(corpus["manifest"]),
        _states(corpus),
        corpus["layout"],
        evaluation_config,
    )


def test_unlabeled_report_states_it_has_no_accuracy_metrics(
    corpus, evaluation_config
):
    report = _unlabeled(corpus, evaluation_config)
    assert report["gold_metrics_available"] is False
    assert report["gold_metrics"] is None
    for warning in evaluator.UNLABELED_WARNINGS:
        assert warning in report["warnings"]
    assert any("CANNOT SUPPORT ANY" in warning for warning in report["warnings"])


def test_unlabeled_report_contains_no_accuracy_vocabulary(corpus, evaluation_config):
    """Test 27: an execution report must not carry a metric that reads as
    accuracy, in any key, at any depth."""
    report = _unlabeled(corpus, evaluation_config)

    def _keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from _keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from _keys(item)

    keys = set(_keys(report))
    for forbidden in ("precision", "recall", "accuracy", "f1", "gold_score"):
        assert not any(forbidden in key for key in keys), forbidden


def test_unlabeled_report_includes_execution_mechanics(corpus, evaluation_config):
    report = _unlabeled(corpus, evaluation_config)
    execution = report["executions"][0]
    assert execution["built"] is True
    assert execution["executed"] is True
    assert execution["lifecycle"] == "detected"
    assert execution["change_count"] >= 1
    assert set(execution["changes_by_type"]) <= {
        "added", "removed", "modified", "undetermined"
    }
    assert execution["undetermined_count"] == execution["changes_by_type"].get(
        "undetermined", 0
    )
    mechanics = report["evidence_resolution_mechanics"]
    assert mechanics["evidence_total"] >= 1
    assert mechanics["evidence_unresolved"] == 0
    assert "not a measure of whether the change was right" in mechanics["note"]


def test_unlabeled_report_ignores_machine_proposed_annotations(
    corpus, evaluation_config
):
    rfb.write_json_atomic(
        corpus["layout"].annotation_path("pair-01"), corpus["machine_annotation"]
    )
    report = _unlabeled(corpus, evaluation_config)
    assert report["corpus_quality"]["pairs_human_verified"] == 0
    assert report["corpus_quality"]["pairs_machine_proposed_only"] == 1
    assert report["gold_metrics"] is None


# --- Operational metrics ------------------------------------------------------


def test_operational_metrics_use_nearest_rank_percentiles(corpus, evaluation_config):
    import comparison_reliability

    _verify(corpus)
    operational = _report(corpus, evaluation_config)["operational"]
    assert operational["percentile_method"] == comparison_reliability.PERCENTILE_METHOD
    assert operational["percentile_method"] == "nearest_rank"
    assert operational["detection_duration_sample_size"] >= 1
    assert operational["total_detection_attempts"] >= 1


def test_nearest_rank_percentile_is_deterministic_for_small_samples():
    import comparison_reliability

    assert comparison_reliability.percentile([5.0], 0.95) == 5.0
    assert comparison_reliability.percentile([1.0, 2.0, 3.0, 4.0], 0.50) == 2.0
    assert comparison_reliability.percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0
    assert comparison_reliability.percentile([], 0.50) is None


def test_operational_metrics_state_that_no_job_path_was_exercised(
    corpus, evaluation_config
):
    _verify(corpus)
    operational = _report(corpus, evaluation_config)["operational"]
    assert operational["detection_jobs"] == 0
    assert operational["lease_reclaims"] == 0
    assert "not that it succeeded" in operational["execution_mode"]


def test_failures_are_reported_by_stable_code(tmp_path, evaluation_config):
    document = fx.single_pair_manifest(current_html=fx.NO_SECTION_HTML)
    manifest_path = fx.write_manifest(tmp_path, document)
    layout = rfb.CorpusLayout(tmp_path / "benchmark_data")
    fx.seed_sources(
        layout,
        document,
        {
            ("pair-01", "previous"): fx.PREVIOUS_HTML,
            ("pair-01", "current"): fx.NO_SECTION_HTML,
        },
    )
    builder.build_pair(document["pairs"][0], document, layout)
    states = [
        evaluator.collect_pair_state(pair, layout)
        for pair in rfb.manifest_pairs(document)
    ]
    report = evaluator.unlabeled_report(
        document, manifest_path, rfb.manifest_pairs(document), states, layout,
        evaluation_config,
    )
    assert isinstance(report["operational"]["failures_by_code"], dict)


# --- CLI and output hygiene ---------------------------------------------------


def test_cli_exits_nonzero_when_gold_evaluation_is_refused(corpus, capsys):
    code = evaluator.main(
        [
            "--manifest",
            str(corpus["manifest_path"]),
            "--corpus-dir",
            str(corpus["layout"].root),
        ]
    )
    assert code == 1
    assert "GOLD EVALUATION REFUSED" in capsys.readouterr().out


def test_cli_exits_zero_on_a_human_verified_corpus(corpus, capsys):
    _verify(corpus)
    code = evaluator.main(
        [
            "--manifest",
            str(corpus["manifest_path"]),
            "--corpus-dir",
            str(corpus["layout"].root),
        ]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "Gold metrics" in output
    assert "human-verified pairs only" in output


def test_cli_output_contains_no_absolute_paths_or_filing_content(corpus, capsys):
    _verify(corpus)
    evaluator.main(
        [
            "--manifest",
            str(corpus["manifest_path"]),
            "--corpus-dir",
            str(corpus["layout"].root),
            "--json",
        ]
    )
    output = capsys.readouterr().out
    assert str(corpus["layout"].root) not in output
    assert "<html" not in output
    assert "attempted intrusion events" not in output
    for secret in ("FDIA_AUTH_SECRET", "SEC_USER_AGENT", "AWS_SECRET"):
        assert secret not in output


def test_cli_refuses_an_unresolved_manifest(capsys):
    assert evaluator.main([]) == 2
    assert "no resolved pairs" in capsys.readouterr().err


def test_cli_rejects_unknown_pair_ids(corpus, capsys):
    code = evaluator.main(
        [
            "--manifest",
            str(corpus["manifest_path"]),
            "--corpus-dir",
            str(corpus["layout"].root),
            "--pair-id",
            "pair-99",
        ]
    )
    assert code == 2
    assert "Unknown pair ids" in capsys.readouterr().err


# --- CI wiring ----------------------------------------------------------------


def _workflow():
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def test_required_check_runs_the_offline_benchmark_suites():
    workflow = _workflow()
    job = workflow["jobs"]["comparison-regression"]
    runs = "\n".join(
        step["run"]
        for step in job["steps"]
        if isinstance(step.get("run"), str) and "pytest" in step["run"]
    )
    for suite in BENCHMARK_SUITES:
        assert suite in runs, suite
        assert (REPO_ROOT / suite).is_file(), suite


def test_required_check_identity_is_unchanged():
    """Adding benchmark coverage must not disturb the branch-protection
    contract: same workflow name, job id, job name, triggers, no path filters."""
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


def test_required_check_never_acquires_filings_or_uses_credentials():
    """Test 29: no live SEC network, no secrets, in the merge-blocking check."""
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "fetch_real_filing_benchmark",
        "--allow-network",
        "SEC_USER_AGENT",
        "sec.gov",
        "secrets.",
        "AWS_ACCESS_KEY",
        "configure-aws-credentials",
    ):
        assert forbidden not in raw, forbidden
    job = _workflow()["jobs"]["comparison-regression"]
    assert "env" not in job
    for step in job["steps"]:
        assert "env" not in step


def test_benchmark_suites_never_call_the_real_transport():
    """Test 29 boundary, checked at the source: the offline suites must not
    reach urllib, and the acquisition transport is always injected."""
    import ast

    for suite in BENCHMARK_SUITES:
        source = (REPO_ROOT / suite).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Attribute) and node.attr == "urlopen":
                raise AssertionError(f"{suite} references urlopen")
            if isinstance(node, ast.Name) and node.id == "urllib_transport":
                raise AssertionError(f"{suite} uses the real transport")


def test_evaluator_imports_no_network_client():
    import ast

    source = (
        REPO_ROOT / "scripts" / "eval_real_filing_benchmark.py"
    ).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("urllib", "http", "socket", "requests", "httpx", "boto3"):
        assert forbidden not in imported, forbidden
    assert "real_filing_acquisition" not in imported


# --- Synthetic regression compatibility ---------------------------------------


def test_synthetic_regression_labels_and_baseline_are_untouched():
    """The deterministic gate this commit must not disturb."""
    labels = json.loads(
        (
            REPO_ROOT / "tests" / "fixtures" / "comparison_regression_labels.json"
        ).read_text(encoding="utf-8")
    )
    assert labels["label_schema_version"] == "comparison-regression.v1"
    assert len(labels["fixtures"]) == 5
    baseline = json.loads(
        (
            REPO_ROOT / "eval" / "comparison_regression_baseline.json"
        ).read_text(encoding="utf-8")
    )
    assert baseline["versions"]["detector_version"] == (
        comparison_detector.DETECTOR_VERSION
    )
    assert baseline["versions"]["workflow_version"] == comparison_store.WORKFLOW_VERSION


def test_benchmark_introduces_no_gate_into_the_regression_suite():
    from scripts import eval_comparison_regression as ecr

    assert "real_filing" not in json.dumps(sorted(ecr.GATES))
    assert len(ecr.GATES) == 10
