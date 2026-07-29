"""Absence and behavioral-equivalence tests for removed dead configuration.

Removed as provably unused: config.INGEST_TABLE_EXTRACTION (declared, never
read), the FormatHandler.extract_tables / filing_type_hints fields (written,
never read), the risk threshold return_with_warning_below (declared in YAML
and defaults, read by nothing - the medium band is derived from the other two
thresholds), and five context-policy keys that were loaded but never
consulted. These tests pin the absence and prove the behavior that mattered
survives: table extraction still runs, the medium band still exists, and
admission still enforces its real controls.
"""

import os
from dataclasses import fields
from pathlib import Path

import config
from governance.context_policy import ContextPolicy, admit_chunks, load_policy
from governance.risk_scorer import _DEFAULT_THRESHOLDS, THRESHOLDS, score
from loaders.registry import FormatHandler, handler_for

_CORPUS_PDF = Path(__file__).resolve().parent.parent / "docs" / "acme-corp-10k-excerpt-2025.pdf"


def test_removed_config_flag_is_gone():
    assert not hasattr(config, "INGEST_TABLE_EXTRACTION")


def test_format_handler_has_no_dead_fields():
    names = {f.name for f in fields(FormatHandler)}
    assert names == {"extensions", "loader", "splitter", "format_family"}


def test_pdf_table_extraction_is_active_regardless_of_the_former_env_var(monkeypatch):
    """Table extraction never depended on the flag; the env var is inert."""
    if not _CORPUS_PDF.exists():
        import pytest

        pytest.skip("corpus PDF not present")
    monkeypatch.setenv("INGEST_TABLE_EXTRACTION", "false")
    handler = handler_for(".pdf")
    docs = handler.loader(str(_CORPUS_PDF))
    combined = "\n".join(doc.page_content for doc in docs)
    assert "[Extracted Tables]" in combined
    # And identically with the variable absent entirely.
    monkeypatch.delenv("INGEST_TABLE_EXTRACTION")
    docs_again = handler.loader(str(_CORPUS_PDF))
    assert "\n".join(d.page_content for d in docs_again) == combined


def test_risk_thresholds_have_no_dead_band_key_and_medium_band_survives():
    assert "return_with_warning_below" not in _DEFAULT_THRESHOLDS
    assert "return_with_warning_below" not in THRESHOLDS
    # The medium band is derived from the two real thresholds, unchanged:
    # 0.50 <= score < 0.75 -> medium, no mandatory review from the weighted gate.
    result = score(grounding_score=0.6, guardrail_outcome="passed", external_context_used=True)
    assert result["risk_score"] == 0.4
    assert result["risk_level"] == "low"
    result = score(grounding_score=0.5, guardrail_outcome=None, external_context_used=True)
    assert result["risk_score"] == 0.45
    assert result["risk_level"] == "low"
    result = score(grounding_score=0.9, guardrail_outcome="anonymized", external_context_used=True)
    # 0.05 + 0.15 + 0.2 = 0.40 -> low; and a genuinely medium case:
    mid = score(grounding_score=0.0, guardrail_outcome="passed", external_context_used=False)
    assert mid["risk_score"] == 0.5
    assert mid["risk_level"] == "medium"


def test_context_policy_field_set_is_exactly_the_enforced_controls():
    names = {f.name for f in fields(ContextPolicy)}
    assert names == {
        "id",
        "max_total_context_tokens",
        "max_internal_context_tokens",
        "max_external_context_tokens",
        "exclude_expired_documents",
        "exclude_unapproved_documents",
        "min_retrieval_score",
    }
    # The shipped YAML still loads (no removed keys linger in it).
    policy = load_policy()
    assert policy.id == "regulated_doc_agent_v1"


def test_admission_behavior_identical_for_all_active_controls():
    """Selected/dropped results for every enforced control are unchanged."""
    policy = ContextPolicy(
        max_internal_context_tokens=10,
        max_total_context_tokens=10_000,
        min_retrieval_score=0.5,
    )
    chunks = [
        {"chunk_id": "expired", "content": "x", "document_status": "expired"},
        {"chunk_id": "draft", "content": "x", "document_status": "draft"},
        {"chunk_id": "weak", "content": "x", "score": 0.2},
        {"chunk_id": "fits", "content": "a" * 40, "score": 0.9},
        {"chunk_id": "over", "content": "b" * 40, "score": 0.9},
    ]
    selected, drops = admit_chunks(chunks, policy, is_external=False)
    assert [c["chunk_id"] for c in selected] == ["fits"]
    assert [(d.chunk_id, d.reason) for d in drops] == [
        ("expired", "stale_document_version"),
        ("draft", "unapproved_document"),
        ("weak", "low_retrieval_score"),
        ("over", "internal_context_budget_exceeded"),
    ]


def test_old_policy_files_with_removed_keys_still_load(tmp_path):
    """A user's YAML carrying the removed keys loads: unknown keys are ignored
    (context) or carried without effect (risk), never a crash."""
    context_yaml = tmp_path / "context.yaml"
    context_yaml.write_text(
        "context_policy:\n"
        "  id: legacy_v1\n"
        "  require_internal_first: true\n"
        "  preserve_citation_traceability: true\n",
        encoding="utf-8",
    )
    policy = load_policy(str(context_yaml))
    assert policy.id == "legacy_v1"
    assert not hasattr(policy, "require_internal_first")

    from governance.risk_scorer import _load_policy

    risk_yaml = tmp_path / "risk.yaml"
    risk_yaml.write_text(
        "risk_thresholds:\n  return_with_warning_below: 0.75\n", encoding="utf-8"
    )
    thresholds, _ = _load_policy(risk_yaml)
    assert thresholds["return_with_warning_below"] == 0.75  # carried, unread
    assert thresholds["require_review_at_or_above"] == 0.75
