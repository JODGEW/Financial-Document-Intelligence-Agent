"""Validation tests for the governance policy loaders.

Contract under test: a missing policy file falls back to baked-in defaults
(documented), but a present file that is malformed or carries invalid values
raises GovernancePolicyConfigError with a message naming the policy and field
- never a silent default fallback, never an incidental TypeError deferred to
scoring or admission time.
"""

import pytest

from governance import risk_scorer
from governance.context_policy import ContextPolicy, load_policy
from governance.policy_validation import GovernancePolicyConfigError
from governance.risk_scorer import _DEFAULT_THRESHOLDS, _DEFAULT_WEIGHTS, _load_policy


def _write(tmp_path, text):
    target = tmp_path / "policy.yaml"
    target.write_text(text, encoding="utf-8")
    return target


# --- shipped files and documented fallbacks ---------------------------------


def test_checked_in_policy_files_load_successfully():
    """Both shipped YAML files load to the documented effective values."""
    thresholds, weights = _load_policy()
    assert thresholds["auto_return_below"] == 0.50
    assert thresholds["require_review_at_or_above"] == 0.75
    assert thresholds["require_review_below_grounding"] == 0.50
    assert weights == {k: float(v) for k, v in _DEFAULT_WEIGHTS.items()}

    policy = load_policy()
    assert policy.id == "regulated_doc_agent_v1"
    assert policy.max_internal_context_tokens == 10000
    assert policy.min_retrieval_score == 0.0
    assert policy.exclude_expired_documents is True


def test_missing_files_still_fall_back_to_defaults(tmp_path):
    """Absence (not invalidity) keeps the documented default fallback."""
    thresholds, weights = _load_policy(tmp_path / "absent.yaml")
    assert thresholds == _DEFAULT_THRESHOLDS
    assert weights == _DEFAULT_WEIGHTS
    assert load_policy(str(tmp_path / "absent.yaml")) == ContextPolicy()


# --- malformed documents ----------------------------------------------------


@pytest.mark.parametrize("loader", [_load_policy, lambda p: load_policy(str(p))])
def test_invalid_yaml_syntax_raises_config_error(tmp_path, loader):
    target = _write(tmp_path, "risk_thresholds: [unclosed\n  nope: {")
    with pytest.raises(GovernancePolicyConfigError, match="invalid YAML syntax"):
        loader(target)


@pytest.mark.parametrize("loader", [_load_policy, lambda p: load_policy(str(p))])
def test_empty_yaml_raises_config_error(tmp_path, loader):
    target = _write(tmp_path, "# only a comment, no mapping\n")
    with pytest.raises(GovernancePolicyConfigError, match="empty"):
        loader(target)


@pytest.mark.parametrize("loader", [_load_policy, lambda p: load_policy(str(p))])
def test_list_root_raises_config_error(tmp_path, loader):
    target = _write(tmp_path, "- a\n- b\n")
    with pytest.raises(GovernancePolicyConfigError, match="must be a mapping"):
        loader(target)


def test_file_with_no_recognized_section_raises(tmp_path):
    """A present file that configures nothing (e.g. a typo'd section name) fails."""
    target = _write(tmp_path, "risk_treshold:\n  auto_return_below: 0.4\n")
    with pytest.raises(GovernancePolicyConfigError, match="configures nothing"):
        _load_policy(target)

    target2 = _write(tmp_path, "context_polcy:\n  id: x\n")
    with pytest.raises(GovernancePolicyConfigError, match="configures nothing"):
        load_policy(str(target2))


def test_non_mapping_section_raises(tmp_path):
    target = _write(tmp_path, "risk_thresholds: hello\n")
    with pytest.raises(GovernancePolicyConfigError, match="section 'risk_thresholds'"):
        _load_policy(target)

    target2 = _write(tmp_path, "context_policy: [1, 2]\n")
    with pytest.raises(GovernancePolicyConfigError, match="section 'context_policy'"):
        load_policy(str(target2))


# --- value validation: risk thresholds --------------------------------------


def test_string_threshold_raises_instead_of_deferring_a_typeerror(tmp_path):
    target = _write(tmp_path, "risk_thresholds:\n  require_review_at_or_above: high\n")
    with pytest.raises(
        GovernancePolicyConfigError,
        match=r"risk_thresholds\.require_review_at_or_above.*must be a number",
    ):
        _load_policy(target)


def test_boolean_threshold_is_rejected_despite_bool_being_int(tmp_path):
    target = _write(tmp_path, "risk_thresholds:\n  auto_return_below: true\n")
    with pytest.raises(GovernancePolicyConfigError, match="must be a number, got bool"):
        _load_policy(target)


def test_non_finite_threshold_is_rejected(tmp_path):
    target = _write(tmp_path, "risk_thresholds:\n  require_review_at_or_above: .nan\n")
    with pytest.raises(GovernancePolicyConfigError, match="finite"):
        _load_policy(target)
    target2 = _write(tmp_path, "signal_weights:\n  grounding_score_weight: .inf\n")
    with pytest.raises(GovernancePolicyConfigError, match="finite"):
        _load_policy(target2)


def test_out_of_range_threshold_is_rejected(tmp_path):
    target = _write(tmp_path, "risk_thresholds:\n  require_review_at_or_above: 1.5\n")
    with pytest.raises(GovernancePolicyConfigError, match="between 0.0 and 1.0"):
        _load_policy(target)


def test_inconsistent_threshold_ordering_is_rejected(tmp_path):
    """auto_return_below above the review threshold breaks the low/medium/high bands."""
    target = _write(tmp_path, "risk_thresholds:\n  auto_return_below: 0.9\n")
    with pytest.raises(GovernancePolicyConfigError, match="auto_return_below"):
        _load_policy(target)


def test_partial_override_still_merges_defaults(tmp_path):
    """The documented partial-file contract survives validation."""
    target = _write(
        tmp_path,
        "risk_thresholds:\n  require_review_at_or_above: 0.60\n"
        "signal_weights:\n  external_context_weight: 0.4\n",
    )
    thresholds, weights = _load_policy(target)
    assert thresholds["require_review_at_or_above"] == 0.60
    assert thresholds["auto_return_below"] == 0.50  # default retained
    assert weights["external_context_weight"] == 0.4
    assert weights["grounding_score_weight"] == 0.5  # default retained


def test_unknown_risk_keys_are_carried_not_rejected(tmp_path):
    """Forward-compatible unknown keys keep loading (documented contract)."""
    target = _write(
        tmp_path,
        "risk_thresholds:\n  future_gate: 0.9\n  auto_return_below: 0.4\n",
    )
    thresholds, _ = _load_policy(target)
    assert thresholds["future_gate"] == 0.9
    assert thresholds["auto_return_below"] == 0.4


# --- value validation: context policy ----------------------------------------


def test_string_token_budget_raises_instead_of_deferring(tmp_path):
    target = _write(
        tmp_path, "context_policy:\n  max_internal_context_tokens: lots\n"
    )
    with pytest.raises(
        GovernancePolicyConfigError,
        match=r"context_policy\.max_internal_context_tokens.*must be an integer",
    ):
        load_policy(str(target))


def test_negative_token_budget_is_rejected(tmp_path):
    target = _write(
        tmp_path, "context_policy:\n  max_external_context_tokens: -5\n"
    )
    with pytest.raises(GovernancePolicyConfigError, match="must be >= 0"):
        load_policy(str(target))


def test_boolean_token_budget_is_rejected(tmp_path):
    target = _write(tmp_path, "context_policy:\n  max_total_context_tokens: true\n")
    with pytest.raises(GovernancePolicyConfigError, match="must be an integer, got bool"):
        load_policy(str(target))


def test_truthy_string_boolean_is_rejected(tmp_path):
    """A quoted "false" is a truthy string - the silent-inversion trap."""
    target = _write(
        tmp_path, 'context_policy:\n  exclude_expired_documents: "false"\n'
    )
    with pytest.raises(GovernancePolicyConfigError, match="must be a boolean"):
        load_policy(str(target))


def test_out_of_range_min_retrieval_score_is_rejected(tmp_path):
    target = _write(tmp_path, "context_policy:\n  min_retrieval_score: 2.0\n")
    with pytest.raises(GovernancePolicyConfigError, match="between 0.0 and 1.0"):
        load_policy(str(target))


def test_empty_policy_id_is_rejected(tmp_path):
    target = _write(tmp_path, 'context_policy:\n  id: ""\n')
    with pytest.raises(GovernancePolicyConfigError, match="non-empty string"):
        load_policy(str(target))


def test_unknown_context_keys_are_ignored_not_rejected(tmp_path):
    target = _write(
        tmp_path,
        "context_policy:\n  id: custom_v2\n  future_note: anything goes\n",
    )
    policy = load_policy(str(target))
    assert policy.id == "custom_v2"
    assert policy.max_internal_context_tokens == 10000  # default retained


# --- error message hygiene ---------------------------------------------------


def test_error_names_policy_and_field_but_not_the_document(tmp_path):
    """The message identifies what failed without echoing the YAML contents."""
    marker = "UNIQUE-YAML-CONTENT-MARKER-abc123"
    target = _write(
        tmp_path,
        f"# {marker}\n"
        "context_policy:\n"
        f"  exclude_expired_documents: {marker}\n",
    )
    with pytest.raises(GovernancePolicyConfigError) as excinfo:
        load_policy(str(target))
    message = str(excinfo.value)
    assert "context_policy.exclude_expired_documents" in message
    assert "boolean" in message
    assert marker not in message


def test_module_level_policies_loaded_from_valid_files():
    """The import-time singletons exist and reflect the shipped files."""
    assert risk_scorer.THRESHOLDS["require_review_at_or_above"] == 0.75
    from governance.context_policy import POLICY

    assert POLICY.id == "regulated_doc_agent_v1"
