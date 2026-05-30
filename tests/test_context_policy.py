"""Tests for the Context Policy Manager (Governance_layer.md §7.3)."""

from governance.context_policy import (
    ContextPolicy,
    admit_chunks,
    load_policy,
)


def test_load_policy_reads_yaml_field_set():
    """The shipped YAML loads into the §7.3 field set with the expected id."""
    policy = load_policy()
    assert policy.id == "regulated_doc_agent_v1"
    assert policy.max_total_context_tokens == 12000
    assert policy.max_internal_context_tokens == 10000
    assert policy.max_external_context_tokens == 1500
    assert policy.exclude_expired_documents is True
    assert policy.exclude_unapproved_documents is True
    # min_retrieval_score is opt-in; default config does not filter by score.
    assert policy.min_retrieval_score == 0.0


def test_load_policy_falls_back_to_defaults_when_yaml_missing():
    """A missing file yields baked-in defaults instead of crashing (risk_scorer pattern)."""
    policy = load_policy("/nonexistent/context_policy.yaml")
    assert policy == ContextPolicy()
    assert policy.id == "regulated_doc_agent_v1"
    assert policy.max_internal_context_tokens == 10000


def test_internal_token_cap_fires_internal_budget_reason():
    """Internal chunks past max_internal_context_tokens drop with the budget reason."""
    policy = ContextPolicy(max_internal_context_tokens=10, max_total_context_tokens=10_000)
    # approx_tokens uses len // 4, so 40 chars == 10 tokens each.
    chunks = [
        {"chunk_id": "a", "content": "x" * 40},  # 10 tokens, fits exactly
        {"chunk_id": "b", "content": "y" * 40},  # would push to 20, over the cap
    ]
    selected, drops = admit_chunks(chunks, policy, is_external=False)

    assert [c["chunk_id"] for c in selected] == ["a"]
    assert [d.reason for d in drops] == ["internal_context_budget_exceeded"]
    assert drops[0].chunk_id == "b"


def test_low_retrieval_score_drops_when_threshold_set():
    """With min_retrieval_score > 0, a below-threshold chunk drops as low_retrieval_score."""
    policy = ContextPolicy(min_retrieval_score=0.5)
    chunks = [
        {"chunk_id": "keep", "content": "relevant", "score": 0.8},
        {"chunk_id": "drop", "content": "weak", "score": 0.2},
    ]
    selected, drops = admit_chunks(chunks, policy, is_external=False)

    assert [c["chunk_id"] for c in selected] == ["keep"]
    assert [d.reason for d in drops] == ["low_retrieval_score"]


def test_expired_document_drops_with_stale_reason():
    """A chunk whose metadata marks the document expired drops as stale_document_version."""
    policy = ContextPolicy()
    chunks = [
        {"chunk_id": "current", "content": "active text"},
        {"chunk_id": "old", "content": "stale text", "metadata": {"document_status": "expired"}},
    ]
    selected, drops = admit_chunks(chunks, policy, is_external=False)

    assert [c["chunk_id"] for c in selected] == ["current"]
    assert [d.reason for d in drops] == ["stale_document_version"]


def test_draft_document_drops_with_unapproved_reason():
    """A draft document chunk drops as unapproved_document."""
    policy = ContextPolicy()
    chunks = [
        {"chunk_id": "approved", "content": "published text"},
        {"chunk_id": "wip", "content": "draft text", "document_status": "draft"},
    ]
    selected, drops = admit_chunks(chunks, policy, is_external=False)

    assert [c["chunk_id"] for c in selected] == ["approved"]
    assert [d.reason for d in drops] == ["unapproved_document"]
