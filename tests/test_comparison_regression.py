"""Tests for the controlled comparison regression suite and its CI gate.

Runs the real evaluator over the real labeled fixtures (offline: temporary
registry/index/SQLite per fixture, no AWS credentials, no network, no LLM),
then exercises label validation, metric sensitivity, determinism, report
hygiene, baseline policy, exit codes, and the GitHub Actions wiring.

Metric-sensitivity tests deliberately inject synthetic score rows rather than
degrading the detector: the point is that a regression WOULD be caught, and
proving that must not require changing production behavior.
"""

import copy
import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

from scripts import eval_comparison_regression as ecr

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "comparison-regression.yml"
CHUNK_FIXTURE_IDS = (
    "no-change-pair",
    "missing-section-pair",
    "ambiguous-heading-pair",
    "retitled-unit-pair",
)


@pytest.fixture(scope="module")
def labels():
    return ecr.load_labels()


@pytest.fixture(scope="module")
def report(labels):
    """One full suite run, shared across tests (each fixture still runs from
    its own clean temporary state inside run_suite)."""
    return ecr.run_suite(copy.deepcopy(labels))


@pytest.fixture(scope="module")
def chunk_outcomes(labels, tmp_path_factory):
    """Raw outcomes for the four fast chunk fixtures (no PDF ingestion)."""
    outcomes = {}
    for fixture in labels["fixtures"]:
        if fixture["fixture_id"] not in CHUNK_FIXTURE_IDS:
            continue
        workdir = tmp_path_factory.mktemp(fixture["fixture_id"])
        outcomes[fixture["fixture_id"]] = ecr.run_fixture(fixture, workdir)
    return outcomes


def _fixture_label(labels, fixture_id):
    return next(
        item for item in labels["fixtures"] if item["fixture_id"] == fixture_id
    )


def _score(labels, fixture_id, outcomes):
    return ecr.score_fixture(
        _fixture_label(labels, fixture_id), outcomes[fixture_id]
    )


def _synthetic_score(**overrides):
    """A perfect single-fixture score row; overrides inject one regression."""
    base = {
        "fixture_id": "synthetic",
        "expected_change_count": 2,
        "detected_change_count": 2,
        "true_positives": [("a", "modified"), ("b", "added")],
        "false_positives": [],
        "missed": [],
        "matched_expected_count": 2,
        "type_correct_count": 2,
        "unchanged_units": 1,
        "unchanged_false_positives": [],
        "evidence_total": 3,
        "evidence_unresolved": 0,
        "evidence_foreign": 0,
        "evidence_sides_ok": True,
        "undetermined_reason_total": 1,
        "undetermined_reason_correct": 1,
        "lifecycle": "detected",
        "lifecycle_ok": True,
        "governance_decision": "returned",
        "governance_ok": True,
        "release_ok": True,
        "invariants": {},
    }
    base.update(overrides)
    return base


# --- Label contract (tests 1-3) ----------------------------------------------


def test_label_schema_accepts_every_checked_in_fixture(labels):
    """Test 1: the checked-in labels validate, and every fixture is declared
    synthetic with a resolvable backing source."""
    assert labels["label_schema_version"] == ecr.LABEL_SCHEMA_VERSION
    assert len(labels["fixtures"]) == 5
    for fixture in labels["fixtures"]:
        assert fixture["synthetic"] is True
        if fixture["kind"] == "chunks":
            assert ecr._fixture_file(fixture).exists()
    # Every fixture file states plainly that it is controlled/synthetic.
    for path in sorted(ecr.FIXTURES_DIR.glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        assert spec["synthetic"] is True
        assert "SYNTHETIC" in spec["notice"].upper()


def test_standard_pair_labels_agree_with_detector_suite_labels(labels):
    """The pre-existing controlled label file and the regression labels must
    not drift apart on the shared standard pair."""
    legacy = json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "comparison_item1a_labels.json").read_text(
            encoding="utf-8"
        )
    )
    standard = _fixture_label(labels, "standard-pair")
    assert standard["previous_filing"]["filing_id"] == legacy["previous_filing_id"]
    assert standard["current_filing"]["filing_id"] == legacy["current_filing_id"]
    assert {
        (change["unit_key"], change["change_type"])
        for change in standard["expected_changes"]
    } == {
        (change["unit_key"], change["change_type"])
        for change in legacy["expected_changes"]
    }
    assert {
        unit["unit_key"] for unit in standard["expected_unchanged_units"]
    } == {unit["unit_key"] for unit in legacy["expected_unchanged_units"]}


@pytest.mark.parametrize(
    "mutate, message_fragment",
    [
        (lambda labels: labels.update({"label_schema_version": "nope"}),
         "label_schema_version"),
        (lambda labels: labels.update({"fixtures": []}), "non-empty"),
        # Duplicate fixture id.
        (lambda labels: labels["fixtures"].append(
            copy.deepcopy(labels["fixtures"][0])), "duplicate fixture_id"),
        # Duplicate expected change key.
        (lambda labels: labels["fixtures"][0]["expected_changes"].append(
            copy.deepcopy(labels["fixtures"][0]["expected_changes"][0])),
         "duplicate expected change key"),
        # Unsupported change type.
        (lambda labels: labels["fixtures"][0]["expected_changes"][0].update(
            {"change_type": "reworded"}), "unsupported change_type"),
        # Missing filing identity.
        (lambda labels: labels["fixtures"][0]["previous_filing"].pop("filing_id"),
         "previous_filing.filing_id is required"),
        # Invalid chronology.
        (lambda labels: labels["fixtures"][0]["previous_filing"].update(
            {"period_end": "2099-12-31"}), "strictly before"),
        # Identical filings.
        (lambda labels: labels["fixtures"][0]["current_filing"].update(
            {"filing_id": labels["fixtures"][0]["previous_filing"]["filing_id"]}),
         "two distinct filings"),
        # Company mismatch.
        (lambda labels: labels["fixtures"][0]["current_filing"].update(
            {"company_key": "other corp"}), "share a company_key"),
        # A unit labeled both changed and unchanged.
        (lambda labels: labels["fixtures"][0]["expected_unchanged_units"].append(
            {"unit_key": labels["fixtures"][0]["expected_changes"][0]["unit_key"],
             "why": "contradiction"}), "both changed and unchanged"),
        # Wrong evidence sides for the change type.
        (lambda labels: labels["fixtures"][0]["expected_changes"][3].update(
            {"evidence_sides": ["previous"]}), "requires evidence_sides"),
        # Undetermined without a reason code.
        (lambda labels: _fixture_label_in(labels, "ambiguous-heading-pair")
            ["expected_changes"][0].update({"undetermined_reason_code": None}),
         "require an"),
        # A held comparison claimed release-eligible before review.
        (lambda labels: _fixture_label_in(labels, "missing-section-pair")
            ["expected_release"].update({"eligible_before_review": True}),
         "cannot be release-eligible"),
        # A rejected review claimed to release.
        (lambda labels: _fixture_label_in(labels, "ambiguous-heading-pair")
            ["expected_release"].update({"eligible_after_review": True}),
         "only an approved review"),
        # A returned comparison routed through a review decision.
        (lambda labels: _fixture_label_in(labels, "no-change-pair")
            ["expected_release"].update({"review_decision": "approved"}),
         "has no review item"),
        # A returned comparison with the wrong release basis.
        (lambda labels: _fixture_label_in(labels, "no-change-pair")
            ["expected_release"].update({"release_basis": "approved_after_review"}),
         "releases under"),
        # An unknown governance decision.
        (lambda labels: labels["fixtures"][0].update(
            {"expected_governance_decision": "approved"}),
         "must be a policy"),
        # A fixture that does not declare itself synthetic.
        (lambda labels: labels["fixtures"][0].update({"synthetic": False}),
         "labeled synthetic"),
    ],
)
def test_invalid_and_contradictory_labels_rejected(labels, mutate, message_fragment):
    """Tests 2-3: every documented label defect raises, with a message that
    names the problem."""
    broken = copy.deepcopy(labels)
    mutate(broken)
    with pytest.raises(ecr.LabelError) as excinfo:
        ecr.validate_labels(broken)
    assert message_fragment in str(excinfo.value)


def _fixture_label_in(labels, fixture_id):
    return next(
        item for item in labels["fixtures"] if item["fixture_id"] == fixture_id
    )


# --- Measured behavior per scenario (tests 4-8) -------------------------------


def test_standard_pair_metrics_are_exact(report):
    """Test 4: the happy-path pair scores perfectly on every dimension."""
    score = next(
        item for item in report["fixtures"] if item["fixture_id"] == "standard-pair"
    )
    assert score["expected_change_count"] == 5
    assert score["detected_change_count"] == 5
    assert len(score["true_positives"]) == 5
    assert score["false_positives"] == []
    assert score["missed"] == []
    assert score["type_correct_count"] == score["matched_expected_count"] == 5
    assert score["unchanged_false_positives"] == []
    assert score["evidence_unresolved"] == 0
    assert score["evidence_foreign"] == 0
    assert score["evidence_sides_ok"] is True
    assert score["lifecycle_ok"] is True
    assert score["governance_decision"] == "returned"
    assert score["export_release_basis"] == "returned_by_policy"
    assert all(score["invariants"].values())


def test_no_change_fixture_feeds_the_false_positive_denominator(labels, chunk_outcomes):
    """Test 5: a zero-expected-change fixture is not dropped — its unchanged
    units are counted in the false-positive denominator and stay at zero."""
    score = _score(labels, "no-change-pair", chunk_outcomes)
    assert score["expected_change_count"] == 0
    assert score["detected_change_count"] == 0
    assert score["unchanged_units"] == 3
    assert score["unchanged_false_positives"] == []
    metrics = ecr.aggregate_metrics([score], {"no-change-pair": True})
    fp = metrics["unchanged_false_positive_rate"]
    assert fp["denominator"] == 3
    assert fp["value"] == 0.0
    # Precision/recall have no changes at all here: explicit, not silent.
    assert metrics["change_precision"]["denominator"] == 0
    assert metrics["change_precision"]["zero_denominator"] is True


def test_missing_section_never_becomes_mass_added_or_removed(
    labels, chunk_outcomes
):
    """Test 6: the previous filing's two units must NOT surface as two removed
    changes when the current section is unavailable."""
    outcome = chunk_outcomes["missing-section-pair"]
    types = [change["change_type"] for change in outcome["changes"]]
    assert types == ["undetermined"]
    assert "removed" not in types and "added" not in types
    assert outcome["changes"][0]["undetermined_reason"].startswith(
        comparison_reason := "current_section_missing"
    )
    assert comparison_reason in outcome["changes"][0]["undetermined_reason"]
    # A data-shaped gap is an undetermined RESULT, not a failed comparison.
    assert outcome["lifecycle"] == "detected"
    assert _score(labels, "missing-section-pair", chunk_outcomes)["release_ok"] is True


def test_ambiguous_heading_reason_is_exact(labels, chunk_outcomes):
    """Test 7: the duplicate-heading fixture yields exactly one undetermined
    change carrying the ambiguous_unit_alignment reason."""
    outcome = chunk_outcomes["ambiguous-heading-pair"]
    assert len(outcome["changes"]) == 1
    change = outcome["changes"][0]
    assert change["change_type"] == "undetermined"
    assert change["undetermined_reason"].startswith("ambiguous_unit_alignment")
    score = _score(labels, "ambiguous-heading-pair", chunk_outcomes)
    assert score["undetermined_reason_correct"] == score["undetermined_reason_total"] == 1
    assert score["unchanged_false_positives"] == []


def test_retitled_unit_matches_documented_limitation(labels, chunk_outcomes):
    """Test 8: a retitled-and-reworded unit surfaces as removed+added, which is
    the documented v1 contract — the label says so instead of the detector
    being loosened to call it modified."""
    fixture = _fixture_label(labels, "retitled-unit-pair")
    assert "limitation" in fixture
    assert "no similarity stage" in fixture["limitation"]
    types = sorted(
        change["change_type"]
        for change in chunk_outcomes["retitled-unit-pair"]["changes"]
    )
    assert types == ["added", "removed"]
    score = _score(labels, "retitled-unit-pair", chunk_outcomes)
    assert score["false_positives"] == []
    assert score["missed"] == []
    assert score["governance_decision"] == "returned"


# --- Metric definitions and sensitivity (tests 9-14) -------------------------


def test_zero_denominator_behavior_is_explicit():
    """Test 9: a zero-denominator rate is null with its policy stated, never
    NaN, never 0.0, and never dropped — and its gate passes."""
    metric = ecr._rate(0, 0, "change_recall")
    assert metric["value"] is None
    assert metric["denominator"] == 0
    assert metric["zero_denominator"] is True
    assert metric["zero_denominator_policy"] == ecr.ZERO_DENOMINATOR_POLICY

    empty = _synthetic_score(
        expected_change_count=0, detected_change_count=0, true_positives=[],
        matched_expected_count=0, type_correct_count=0, unchanged_units=0,
        evidence_total=0, undetermined_reason_total=0,
        undetermined_reason_correct=0,
    )
    metrics = ecr.aggregate_metrics([empty], {"synthetic": True})
    for name in (
        "change_precision", "change_recall", "change_type_accuracy",
        "evidence_resolution_rate", "unchanged_false_positive_rate",
        "undetermined_reason_accuracy",
    ):
        assert name in metrics, name
        assert metrics[name]["value"] is None, name
    gates = {gate["gate"]: gate for gate in ecr.evaluate_gates(metrics)}
    assert all(gate["passed"] for gate in gates.values())
    assert "zero denominator" in gates["change_precision"]["note"]


def test_false_positives_reduce_precision():
    """Test 10."""
    score = _synthetic_score(
        detected_change_count=3, false_positives=[("ghost", "added")]
    )
    metrics = ecr.aggregate_metrics([score], {"synthetic": True})
    assert metrics["change_precision"]["value"] == pytest.approx(2 / 3)
    assert metrics["change_recall"]["value"] == 1.0
    gates = {gate["gate"]: gate for gate in ecr.evaluate_gates(metrics)}
    assert gates["change_precision"]["passed"] is False


def test_missed_labels_reduce_recall():
    """Test 11."""
    score = _synthetic_score(
        expected_change_count=3, missed=[("c", "removed")]
    )
    metrics = ecr.aggregate_metrics([score], {"synthetic": True})
    assert metrics["change_recall"]["value"] == pytest.approx(2 / 3)
    gates = {gate["gate"]: gate for gate in ecr.evaluate_gates(metrics)}
    assert gates["change_recall"]["passed"] is False


def test_wrong_change_type_reduces_type_accuracy():
    """Test 12."""
    score = _synthetic_score(type_correct_count=1)
    metrics = ecr.aggregate_metrics([score], {"synthetic": True})
    assert metrics["change_type_accuracy"]["value"] == 0.5
    gates = {gate["gate"]: gate for gate in ecr.evaluate_gates(metrics)}
    assert gates["change_type_accuracy"]["passed"] is False


def test_unresolved_and_foreign_evidence_reduce_resolution_rate():
    """Test 13: an evidence ref that does not resolve, and one owned by the
    wrong filing, both lower the rate."""
    unresolved = _synthetic_score(evidence_unresolved=1)
    metrics = ecr.aggregate_metrics([unresolved], {"synthetic": True})
    assert metrics["evidence_resolution_rate"]["value"] == pytest.approx(2 / 3)

    foreign = _synthetic_score(evidence_foreign=1)
    metrics = ecr.aggregate_metrics([foreign], {"synthetic": True})
    assert metrics["evidence_resolution_rate"]["value"] == pytest.approx(2 / 3)
    gates = {gate["gate"]: gate for gate in ecr.evaluate_gates(metrics)}
    assert gates["evidence_resolution_rate"]["passed"] is False


def test_wrong_undetermined_code_reduces_reason_accuracy(labels, chunk_outcomes):
    """Test 14: measured end to end — relabeling the expected reason code makes
    the real detector output score as wrong."""
    mutated = copy.deepcopy(_fixture_label(labels, "ambiguous-heading-pair"))
    mutated["expected_changes"][0]["undetermined_reason_code"] = (
        "previous_section_missing"
    )
    score = ecr.score_fixture(mutated, chunk_outcomes["ambiguous-heading-pair"])
    assert score["undetermined_reason_total"] == 1
    assert score["undetermined_reason_correct"] == 0
    metrics = ecr.aggregate_metrics([score], {"x": True})
    assert metrics["undetermined_reason_accuracy"]["value"] == 0.0
    gates = {gate["gate"]: gate for gate in ecr.evaluate_gates(metrics)}
    assert gates["undetermined_reason_accuracy"]["passed"] is False


def test_unchanged_unit_emitted_as_change_is_a_false_positive():
    """A unit labeled unchanged that shows up as a change fails its gate."""
    score = _synthetic_score(unchanged_false_positives=["item-1a-preamble"])
    metrics = ecr.aggregate_metrics([score], {"synthetic": True})
    assert metrics["unchanged_false_positive_rate"]["value"] == 1.0
    gates = {gate["gate"]: gate for gate in ecr.evaluate_gates(metrics)}
    assert gates["unchanged_false_positive_rate"]["passed"] is False


# --- Workflow-state mismatches (tests 15-17) ---------------------------------


def test_lifecycle_mismatch_fails(labels, chunk_outcomes):
    """Test 15: measured against real output — expecting 'failed' when the
    workflow legitimately detected is a scored failure."""
    mutated = copy.deepcopy(_fixture_label(labels, "missing-section-pair"))
    mutated["expected_lifecycle"] = "failed"
    score = ecr.score_fixture(mutated, chunk_outcomes["missing-section-pair"])
    assert score["lifecycle_ok"] is False
    metrics = ecr.aggregate_metrics([score], {"x": True})
    assert metrics["lifecycle_accuracy"]["value"] == 0.0
    gates = {gate["gate"]: gate for gate in ecr.evaluate_gates(metrics)}
    assert gates["lifecycle_accuracy"]["passed"] is False


def test_governance_mismatch_fails(labels, chunk_outcomes):
    """Test 16."""
    mutated = copy.deepcopy(_fixture_label(labels, "no-change-pair"))
    mutated["expected_governance_decision"] = "held_for_review"
    score = ecr.score_fixture(mutated, chunk_outcomes["no-change-pair"])
    assert score["governance_ok"] is False
    metrics = ecr.aggregate_metrics([score], {"x": True})
    assert metrics["governance_decision_accuracy"]["value"] == 0.0
    gates = {gate["gate"]: gate for gate in ecr.evaluate_gates(metrics)}
    assert gates["governance_decision_accuracy"]["passed"] is False


def test_release_eligibility_mismatch_fails(labels, chunk_outcomes):
    """Test 17: both directions — claiming a pending held result releases, and
    claiming a returned result does not."""
    held = copy.deepcopy(_fixture_label(labels, "ambiguous-heading-pair"))
    held["expected_release"]["eligible_before_review"] = True
    score = ecr.score_fixture(held, chunk_outcomes["ambiguous-heading-pair"])
    assert score["release_ok"] is False

    returned = copy.deepcopy(_fixture_label(labels, "retitled-unit-pair"))
    returned["expected_release"]["eligible_before_review"] = False
    score = ecr.score_fixture(returned, chunk_outcomes["retitled-unit-pair"])
    assert score["release_ok"] is False

    wrong_basis = copy.deepcopy(_fixture_label(labels, "retitled-unit-pair"))
    wrong_basis["expected_release"]["release_basis"] = (
        "returned_with_warning_by_policy"
    )
    score = ecr.score_fixture(wrong_basis, chunk_outcomes["retitled-unit-pair"])
    assert score["release_ok"] is False
    metrics = ecr.aggregate_metrics([score], {"x": True})
    gates = {gate["gate"]: gate for gate in ecr.evaluate_gates(metrics)}
    assert gates["release_eligibility_accuracy"]["passed"] is False


def test_release_gate_codes_observed_end_to_end(labels, chunk_outcomes):
    """The release probes record the real refusal codes: pending before a
    decision, rejected after a rejection."""
    ambiguous = chunk_outcomes["ambiguous-heading-pair"]
    assert ambiguous["release_before_review"] == {
        "eligible": False, "code": "review_pending", "release_basis": None
    }
    assert ambiguous["release_after_review"]["code"] == "review_rejected"
    assert "export_id" not in ambiguous

    approved = chunk_outcomes["missing-section-pair"]
    assert approved["release_before_review"]["code"] == "review_pending"
    assert approved["release_after_review"]["eligible"] is True
    assert approved["export_release_basis"] == "approved_after_review"
    assert approved["export_selected_reviewed_snapshot"] is True

    returned = chunk_outcomes["no-change-pair"]
    assert returned["release_before_review"]["release_basis"] == "returned_by_policy"
    assert returned["export_selected_governed_snapshot"] is True
    assert returned["pending_review_count"] == 0


def test_workflow_invariants_hold_for_every_fixture(report):
    """G's end-to-end invariants: idempotency, snapshot preservation, one
    pending review per held result, SQLite integrity."""
    assert report["invariant_failures"] == []
    for score in report["fixtures"]:
        invariants = score["invariants"]
        assert invariants["detect_idempotent"] is True
        assert invariants["govern_idempotent"] is True
        assert invariants["sqlite_integrity_ok"] is True
        assert invariants["workflow_rows_unchanged_after_export"] is True
        assert invariants["evidence_ownership_correct"] is True
        assert invariants["no_not_run_checks"] is True
    held = [
        score
        for score in report["fixtures"]
        if score["governance_decision"] == "held_for_review"
    ]
    assert len(held) == 2
    for score in held:
        assert score["invariants"]["exactly_one_pending_review"] is True


# --- Determinism and report hygiene (tests 18-20) ----------------------------


def test_repeated_clean_runs_are_equivalent(report, labels):
    """Test 18: a second full suite run produces an identical baseline view,
    including every fixture's stable detector result hash."""
    assert all(report["determinism"].values())
    second = ecr.run_suite(copy.deepcopy(labels))
    assert ecr.baseline_view(second) == ecr.baseline_view(report)
    assert ecr.diff_baseline(
        ecr.baseline_view(report), ecr.baseline_view(second)
    ) == []


def test_report_contains_no_absolute_or_temporary_paths(report):
    """Test 19."""
    serialized = json.dumps(report)
    for needle in (
        str(REPO_ROOT), "/private/", "/var/folders", "/tmp/", "cmp-regress-",
        "C:\\\\", ".venv",
    ):
        assert needle not in serialized, needle
    # No absolute POSIX path segments at all.
    assert not re.search(r'"/[A-Za-z0-9_./-]{6,}"', serialized)
    # And nothing that leaks reviewer prose or fixture body text.
    assert ecr._APPROVE_NOTE not in serialized
    assert ecr._REJECT_NOTE not in serialized
    assert "fulfilment centre" not in serialized


def test_report_carries_required_provenance(report):
    """E.12: schema/detector/workflow/validator versions, fixture hashes,
    denominators, and gate results all present."""
    versions = report["versions"]
    assert versions["label_schema_version"] == "comparison-regression.v1"
    assert versions["comparison_schema_version"] == "comparison.v1"
    assert versions["export_schema_version"] == "comparison.export.v1"
    assert versions["detector_version"] == "item1a_detector.v2"
    assert versions["workflow_version"] == "comparison_workflow.v2"
    assert set(versions["validator_versions"]) == {
        "citation_support", "numeric_consistency", "direction_consistency"
    }
    assert set(report["fixture_hashes"]) == {"labels"} | {
        score["fixture_id"] for score in report["fixtures"]
    }
    assert all(len(digest) == 64 for digest in report["fixture_hashes"].values())
    assert all("denominator" in metric for metric in report["metrics"].values())
    assert {gate["gate"] for gate in report["gates"]} == set(ecr.GATES)
    assert "NOT a real-filing benchmark" in report["scope"]


def test_normal_run_does_not_modify_tracked_files(labels):
    """Test 20: labels, fixtures, and the baseline are untouched by a run."""
    tracked = [ecr.LABELS_PATH, ecr.DEFAULT_BASELINE] + sorted(
        ecr.FIXTURES_DIR.glob("*.json")
    )
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tracked
        if path.exists()
    }
    ecr.run_suite(copy.deepcopy(labels))
    after = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tracked
        if path.exists()
    }
    assert after == before


# --- Baseline policy and exit codes (tests 21-24) ----------------------------


def test_baseline_matches_the_checked_in_file(report):
    """The committed baseline is the current behavior — a label, threshold, or
    detector change shows up as an ordinary reviewable diff."""
    assert ecr.DEFAULT_BASELINE.exists()
    stored = json.loads(ecr.DEFAULT_BASELINE.read_text(encoding="utf-8"))
    assert ecr.diff_baseline(stored, ecr.baseline_view(report)) == []


def test_baseline_written_only_with_the_dedicated_flag(tmp_path, monkeypatch):
    """Test 21."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CONTINUOUS_INTEGRATION", raising=False)
    baseline = tmp_path / "baseline.json"

    assert ecr.main(["--json", "--baseline", str(baseline)]) == 0
    assert not baseline.exists()

    assert ecr.main(["--json", "--baseline", str(baseline), "--write-baseline"]) == 0
    assert baseline.exists()
    written = json.loads(baseline.read_text(encoding="utf-8"))
    assert written["suite"] == "comparison-regression"
    assert set(written["metrics"]) == set(ecr.GATES)


@pytest.mark.parametrize(
    "variable", ["CI", "GITHUB_ACTIONS", "CONTINUOUS_INTEGRATION"]
)
def test_baseline_writing_refuses_in_ci(tmp_path, monkeypatch, capsys, variable):
    """Test 22."""
    monkeypatch.setenv(variable, "true")
    baseline = tmp_path / "baseline.json"
    assert ecr.main(["--baseline", str(baseline), "--write-baseline"]) == 2
    assert not baseline.exists()
    assert "Refusing to write the baseline in CI" in capsys.readouterr().err
    assert ecr.in_ci() is True


def test_all_gates_passing_exits_zero(tmp_path, capsys, monkeypatch):
    """Test 24 (plus the report/summary surfaces)."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    report_path = tmp_path / "report.json"
    assert ecr.main(["--report", str(report_path)]) == 0
    out = capsys.readouterr().out
    assert "ALL GATES PASSED" in out
    assert "REGRESSION DETECTED" not in out
    assert "controlled synthetic fixtures" in out
    assert "(numerator/denominator)" in out
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["passed"] is True
    assert str(REPO_ROOT) not in report_path.read_text(encoding="utf-8")


def test_gate_failure_exits_nonzero(monkeypatch, capsys):
    """Test 23: a regressed metric fails the run without touching production
    code — the gate is enforced, not advisory."""
    real_aggregate = ecr.aggregate_metrics

    def regressed(scores, determinism):
        metrics = real_aggregate(scores, determinism)
        metrics["change_recall"] = ecr._rate(1, 2, "change_recall")
        return metrics

    monkeypatch.setattr(ecr, "aggregate_metrics", regressed)
    assert ecr.main([]) == 1
    out = capsys.readouterr().out
    assert "REGRESSION DETECTED" in out
    assert "[FAIL] change_recall" in out


def test_invalid_labels_exit_nonzero(monkeypatch, capsys):
    """A broken label file is a hard failure, not a skipped suite."""
    def broken():
        raise ecr.LabelError("duplicate fixture_id 'x'")

    monkeypatch.setattr(ecr, "load_labels", broken)
    assert ecr.main([]) == 2
    assert "Invalid labels" in capsys.readouterr().err


def test_nondeterministic_output_fails(monkeypatch, capsys):
    """Test 9 of E: nondeterminism is a gate failure, not a warning."""
    real = ecr._determinism_projection
    calls = {"n": 0}

    def unstable(outcome):
        calls["n"] += 1
        projection = dict(real(outcome))
        if calls["n"] % 2 == 0:
            projection["result_hash"] = f"drift-{calls['n']}"
        return projection

    monkeypatch.setattr(ecr, "_determinism_projection", unstable)
    assert ecr.main([]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] deterministic_result_rate" in out
    assert "Nondeterministic fixtures:" in out


# --- CI wiring (tests 25-26) -------------------------------------------------


def test_workflow_invokes_the_exact_evaluator_command():
    """Test 25."""
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert workflow["name"] == "comparison-regression"
    job = workflow["jobs"]["comparison-regression"]
    assert job["name"] == "comparison-regression"  # branch-protection check name
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    assert "python scripts/eval_comparison_regression.py" in runs
    assert "--write-baseline" not in runs
    # Runs on PRs and on pushes to the protected branch.
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert "pull_request" in triggers
    assert triggers["push"]["branches"] == ["main"]
    # No path filters: any dependency of comparison behavior must trigger it,
    # so a filter narrow enough to be useful would be wrong.
    assert not (triggers.get("pull_request") or {}).get("paths")
    assert not (triggers.get("pull_request") or {}).get("paths-ignore")
    assert not (triggers.get("push") or {}).get("paths")
    assert not (triggers.get("push") or {}).get("paths-ignore")


def test_workflow_uses_no_secrets_or_aws_credentials():
    """Test 26."""
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "secrets.", "AWS_ACCESS_KEY", "AWS_SECRET", "AWS_SESSION",
        "aws-actions", "configure-aws-credentials", "BEDROCK",
        "TAVILY", "ANTHROPIC", "OPENAI", "LANGSMITH",
    ):
        assert forbidden not in raw, forbidden
    workflow = yaml.safe_load(raw)
    assert workflow.get("permissions") == {"contents": "read"}
    job = workflow["jobs"]["comparison-regression"]
    assert "env" not in job
    for step in job["steps"]:
        assert "env" not in step
    # Existing repository action versions are reused.
    uses = [step.get("uses") for step in job["steps"] if step.get("uses")]
    assert "actions/checkout@v4" in uses
    assert "actions/setup-python@v5" in uses


def test_evaluator_imports_no_network_or_model_client():
    """Test 27/28 boundary: checked at the import graph, not in prose, so a
    docstring that merely says "no Bedrock" cannot satisfy or break it."""
    import ast

    source = (
        REPO_ROOT / "scripts" / "eval_comparison_regression.py"
    ).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {
        "boto3", "botocore", "requests", "httpx", "urllib", "urllib3",
        "socket", "http", "openai", "anthropic", "langchain_aws", "tavily",
        "aiohttp",
    }
    assert imported & forbidden == set(), imported & forbidden

    # And no attribute path that would reach a model or endpoint at runtime.
    for name in ("ChatBedrock", "BedrockEmbeddings", "invoke_model", "https://"):
        assert name not in source, name
