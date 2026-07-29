"""Shared validation for governance policy YAML files.

Fail-fast contract shared by risk_scorer and context_policy: a MISSING policy
file falls back to the baked-in defaults (documented in both YAML headers and
pinned by tests), but a PRESENT file that is malformed or carries invalid
values raises GovernancePolicyConfigError at import time. Governance
configuration must never be silently replaced by permissive defaults, and it
must not surface later as an incidental TypeError inside scoring or admission.

Error messages name the policy, the field, and the violated constraint. They
include the *type* of an offending value (and the number itself for range
violations, since policy numbers are not sensitive), but never the YAML
document, string values, secrets, or anything from the environment.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml


class GovernancePolicyConfigError(Exception):
    """A governance policy file is present but invalid (operator error)."""


def load_policy_mapping(path: Path, policy_name: str) -> dict[str, Any] | None:
    """Read a policy YAML file into its root mapping.

    Returns None when the file does not exist, which callers treat as "use the
    baked-in defaults" (the documented fallback). Any other failure raises
    GovernancePolicyConfigError: unreadable file, YAML syntax error, empty
    document, or a non-mapping root.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GovernancePolicyConfigError(
            f"{policy_name} policy ({path.name}): file exists but could not be "
            f"read ({type(exc).__name__}). Fix the file or remove it to use the "
            "built-in defaults."
        ) from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" near line {mark.line + 1}" if mark is not None else ""
        raise GovernancePolicyConfigError(
            f"{policy_name} policy ({path.name}): invalid YAML syntax{location}."
        ) from exc

    if raw is None:
        raise GovernancePolicyConfigError(
            f"{policy_name} policy ({path.name}): file is empty. Restore the "
            "documented mapping, or delete the file to use the built-in defaults."
        )
    if not isinstance(raw, dict):
        raise GovernancePolicyConfigError(
            f"{policy_name} policy ({path.name}): top level must be a mapping, "
            f"got {type(raw).__name__}."
        )
    return raw


def require_mapping_section(
    raw: dict[str, Any], section: str, policy_name: str
) -> dict[str, Any] | None:
    """Return raw[section] validated as a mapping; None when absent.

    Absence is the caller's decision (defaults or rejection); presence with a
    non-mapping value is always an error.
    """
    value = raw.get(section)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise GovernancePolicyConfigError(
            f"{policy_name} policy: section '{section}' must be a mapping, "
            f"got {type(value).__name__}."
        )
    return value


def validated_number(
    policy_name: str,
    field_name: str,
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Validate a numeric policy value: real number, not bool, finite, in range."""
    # bool is a subclass of int; `true` must not silently read as 1.0.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GovernancePolicyConfigError(
            f"{policy_name} policy: field '{field_name}' must be a number, "
            f"got {type(value).__name__}."
        )
    number = float(value)
    if not math.isfinite(number):
        raise GovernancePolicyConfigError(
            f"{policy_name} policy: field '{field_name}' must be a finite "
            "number (NaN and infinity are not valid policy values)."
        )
    if (minimum is not None and number < minimum) or (
        maximum is not None and number > maximum
    ):
        raise GovernancePolicyConfigError(
            f"{policy_name} policy: field '{field_name}' must be between "
            f"{minimum} and {maximum}, got {number}."
        )
    return number


def validated_int(
    policy_name: str, field_name: str, value: Any, *, minimum: int | None = None
) -> int:
    """Validate an integer policy value: actual int, not bool, at least minimum."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GovernancePolicyConfigError(
            f"{policy_name} policy: field '{field_name}' must be an integer, "
            f"got {type(value).__name__}."
        )
    if minimum is not None and value < minimum:
        raise GovernancePolicyConfigError(
            f"{policy_name} policy: field '{field_name}' must be >= {minimum}, "
            f"got {value}."
        )
    return value


def validated_bool(policy_name: str, field_name: str, value: Any) -> bool:
    """Validate a boolean policy value: actual bool, never a truthy string."""
    if not isinstance(value, bool):
        raise GovernancePolicyConfigError(
            f"{policy_name} policy: field '{field_name}' must be a boolean "
            f"(true/false), got {type(value).__name__}."
        )
    return value


def validated_policy_id(policy_name: str, field_name: str, value: Any) -> str:
    """Validate a policy id: a non-empty string."""
    if not isinstance(value, str) or not value.strip():
        got = type(value).__name__ if not isinstance(value, str) else "empty string"
        raise GovernancePolicyConfigError(
            f"{policy_name} policy: field '{field_name}' must be a non-empty "
            f"string, got {got}."
        )
    return value
