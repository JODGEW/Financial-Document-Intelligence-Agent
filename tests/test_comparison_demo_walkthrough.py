"""Smoke test for the isolated structured-comparison demo act."""

from io import StringIO

from scripts.comparison_demo_walkthrough import run_comparison_demo


def test_comparison_demo_runs_worker_review_and_export_offline(tmp_path):
    output = StringIO()

    summary = run_comparison_demo(workdir=tmp_path, stream=output)

    assert summary["job_status"] == "succeeded"
    assert summary["change_types"] == ["undetermined"]
    assert len(summary["undetermined_reasons"]) == 1
    assert summary["undetermined_reasons"][0].startswith(
        "current_section_missing:"
    )
    assert summary["governance_decision"] == "held_for_review"
    assert summary["review_action"] == "approved"
    assert summary["reviewer_id_basis"] == "local_hs256"
    assert summary["release_basis"] == "approved_after_review"
    assert summary["release_refusal_http_status"] == 409
    assert summary["state_path"] == str(tmp_path.resolve())
    assert summary["export"]["export_schema_version"] == "comparison.export.v1"

    transcript = output.getvalue()
    assert "STEP 1/6 - Filing pair" in transcript
    assert "Item 1A chunks=2" in transcript
    assert "Item 1A chunks=0" in transcript
    assert "STEP 2/6 - Durable detection" in transcript
    assert "STEP 3/6 - Governance decision" in transcript
    assert "Release gate before review: refused" in transcript
    assert "STEP 5/6 - Authenticated human review" in transcript
    assert "STEP 6/6 - Released structured export" in transcript
    assert "Full export JSON:" not in transcript


def test_comparison_demo_can_pause_and_show_export_json(tmp_path):
    output = StringIO()
    pauses = []

    summary = run_comparison_demo(
        workdir=tmp_path,
        stream=output,
        pause_between_steps=True,
        show_json=True,
        input_fn=lambda: pauses.append("continued") or "",
    )

    assert len(pauses) == 5
    assert "Full export JSON:" in output.getvalue()
    assert f'"export_id": "{summary["export_id"]}"' in output.getvalue()
