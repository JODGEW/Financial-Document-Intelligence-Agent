"""Local signed access tokens and policy-derived comparison permissions.

This module is deliberately a narrow authentication boundary:

* PyJWT performs JWT parsing and HS256 signature verification.
* A checked-in policy defines the only accepted issuer, audience, roles, and
  permissions.
* A runtime-only shared secret proves that a token was issued inside the local
  trust boundary.
* :class:`Principal` contains only verified, allowlisted identity fields and
  permissions derived from policy.

Importing this module validates the checked-in policy but does not read or
require the secret.  API construction and the local token issuer call
``Authenticator.from_environment()`` explicitly, so detector, ingestion,
evaluation, and other library-only commands remain credential-free.
"""

from __future__ import annotations

import os
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import jwt
import yaml

from governance.policy_validation import GovernancePolicyConfigError


AUTH_SECRET_ENV = "FDIA_AUTH_SECRET"
AUTH_METHOD = "local_hs256"
AUTH_ALGORITHM = "HS256"

INVALID_ACCESS_TOKEN = "invalid_access_token"
ACCESS_TOKEN_EXPIRED = "access_token_expired"

MIN_AUTH_SECRET_BYTES = 32
MAX_AUTH_SECRET_BYTES = 4096
MAX_ACCESS_TOKEN_CHARS = 8192
MAX_SUBJECT_CHARS = 120
MAX_TOKEN_ID_CHARS = 128

MIN_MAX_TOKEN_TTL_SECONDS = 1
MAX_MAX_TOKEN_TTL_SECONDS = 86_400
MIN_CLOCK_SKEW_SECONDS = 0
MAX_CLOCK_SKEW_SECONDS = 300

_POLICY_NAME = "access_control"
_POLICY_PATH = (
    Path(__file__).resolve().parent / "policies" / "access_control_policy.yaml"
)

# Stable permission vocabulary.  Authorization is an exact membership check;
# there is no prefix, substring, wildcard, or implicit permission matching.
DEFINED_PERMISSIONS: tuple[str, ...] = (
    "comparison.read",
    "comparison.create",
    "comparison.detect",
    "detection_attempt.read",
    "recovery.read",
    "recovery.replay",
    "reliability.read",
    "governance.read",
    "governance.evaluate",
    "review.read",
    "review.decide",
    "export.read",
    "export.create",
)

DEFINED_ROLES: tuple[str, ...] = (
    "viewer",
    "operator",
    "reviewer",
    "exporter",
    "admin",
)

_VIEWER_PERMISSIONS: tuple[str, ...] = (
    "comparison.read",
    "detection_attempt.read",
    "recovery.read",
    "reliability.read",
    "governance.read",
    "review.read",
    "export.read",
)
_OPERATOR_PERMISSIONS: tuple[str, ...] = tuple(
    permission
    for permission in DEFINED_PERMISSIONS
    if permission
    in {
        *_VIEWER_PERMISSIONS,
        "comparison.create",
        "comparison.detect",
        "recovery.replay",
        "governance.evaluate",
    }
)
_REVIEWER_PERMISSIONS: tuple[str, ...] = tuple(
    permission
    for permission in DEFINED_PERMISSIONS
    if permission in {*_VIEWER_PERMISSIONS, "review.decide"}
)
_EXPORTER_PERMISSIONS: tuple[str, ...] = tuple(
    permission
    for permission in DEFINED_PERMISSIONS
    if permission in {*_VIEWER_PERMISSIONS, "export.create"}
)

# The default is used only when the policy file is absent.  It is intentionally
# the same document as policies/access_control_policy.yaml, and tests can compare
# the two loaded policies directly.
_DEFAULT_POLICY: dict[str, Any] = {
    "policy_id": "comparison_access_control_v1",
    "policy_version": "1",
    "auth_method": AUTH_METHOD,
    "issuer": "react-rag-local",
    "audience": "react-rag-comparison-api",
    "algorithm": AUTH_ALGORITHM,
    "max_token_ttl_seconds": 3600,
    "clock_skew_seconds": 30,
    "roles": {
        "viewer": {"permissions": list(_VIEWER_PERMISSIONS)},
        "operator": {"permissions": list(_OPERATOR_PERMISSIONS)},
        "reviewer": {"permissions": list(_REVIEWER_PERMISSIONS)},
        "exporter": {"permissions": list(_EXPORTER_PERMISSIONS)},
        "admin": {"permissions": list(DEFINED_PERMISSIONS)},
    },
}


class AccessControlConfigError(GovernancePolicyConfigError):
    """The local authentication policy or runtime secret is invalid."""


class AccessTokenError(Exception):
    """A sanitized access-token refusal suitable for the public API contract."""

    def __init__(self, code: str, public_message: str):
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message


class TokenIssuanceError(Exception):
    """Invalid local token-issuance input with a safe terminal message."""


@dataclass(frozen=True, slots=True)
class AccessControlPolicy:
    """Validated, immutable access-control configuration."""

    policy_id: str
    policy_version: str
    auth_method: str
    issuer: str
    audience: str
    algorithm: str
    max_token_ttl_seconds: int
    clock_skew_seconds: int
    role_permissions: Mapping[str, tuple[str, ...]]

    @property
    def roles(self) -> tuple[str, ...]:
        """Role names in deterministic policy order."""
        return tuple(self.role_permissions)

    def permissions_for_roles(self, roles: Sequence[str]) -> tuple[str, ...]:
        """Return a deterministic union, rejecting every unknown role."""
        role_set = set(roles)
        if len(role_set) != len(roles) or not role_set:
            raise ValueError("roles must be a non-empty unique sequence")
        if not role_set.issubset(self.role_permissions):
            raise ValueError("roles contain an unknown role")
        granted = {
            permission
            for role in role_set
            for permission in self.role_permissions[role]
        }
        return tuple(
            permission for permission in DEFINED_PERMISSIONS if permission in granted
        )


@dataclass(frozen=True, slots=True)
class Principal:
    """Allowlisted authenticated identity; never contains the bearer token."""

    subject: str
    roles: tuple[str, ...]
    permissions: tuple[str, ...]
    token_id: str
    issued_at: datetime
    expires_at: datetime
    auth_method: str
    policy_id: str
    policy_version: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize only the fixed Principal allowlist."""
        return {
            "subject": self.subject,
            "roles": list(self.roles),
            "permissions": list(self.permissions),
            "token_id": self.token_id,
            "issued_at": _format_utc(self.issued_at),
            "expires_at": _format_utc(self.expires_at),
            "auth_method": self.auth_method,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
        }


class _DuplicatePolicyKey(Exception):
    def __init__(self, line: int | None):
        self.line = line


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader variant that refuses duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "mapping keys must be scalar values",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise _DuplicatePolicyKey(key_node.start_mark.line + 1)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _read_policy_mapping(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        raise AccessControlConfigError(
            f"{_POLICY_NAME} policy ({path.name}): file exists but could not "
            "be read."
        ) from None

    loader = _UniqueKeySafeLoader(text)
    try:
        raw = loader.get_single_data()
    except _DuplicatePolicyKey as exc:
        location = f" near line {exc.line}" if exc.line is not None else ""
        raise AccessControlConfigError(
            f"{_POLICY_NAME} policy ({path.name}): duplicate mapping key"
            f"{location}."
        ) from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" near line {mark.line + 1}" if mark is not None else ""
        raise AccessControlConfigError(
            f"{_POLICY_NAME} policy ({path.name}): invalid YAML syntax"
            f"{location}."
        ) from None
    finally:
        loader.dispose()

    if raw is None:
        raise AccessControlConfigError(
            f"{_POLICY_NAME} policy ({path.name}): file is empty."
        )
    if not isinstance(raw, dict):
        raise AccessControlConfigError(
            f"{_POLICY_NAME} policy ({path.name}): top level must be a mapping."
        )
    return raw


def _config_error(field: str, constraint: str) -> AccessControlConfigError:
    return AccessControlConfigError(
        f"{_POLICY_NAME} policy: field '{field}' {constraint}."
    )


def _required_string(
    raw: Mapping[str, Any],
    field: str,
    *,
    maximum: int = 200,
) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value or value != value.strip():
        raise _config_error(field, "must be a non-empty trimmed string")
    if len(value) > maximum:
        raise _config_error(field, f"must be at most {maximum} characters")
    if _contains_control(value):
        raise _config_error(field, "must not contain control characters")
    return value


def _bounded_policy_int(
    raw: Mapping[str, Any],
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = raw.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _config_error(field, "must be an integer (boolean values are invalid)")
    if not minimum <= value <= maximum:
        raise _config_error(field, f"must be between {minimum} and {maximum}")
    return value


def _reject_unknown_keys(
    raw: Mapping[str, Any], allowed: set[str], *, section: str
) -> None:
    if set(raw) - allowed:
        raise AccessControlConfigError(
            f"{_POLICY_NAME} policy: section '{section}' contains unknown keys."
        )


def _validate_role_permissions(raw: Any) -> Mapping[str, tuple[str, ...]]:
    if not isinstance(raw, dict):
        raise _config_error("roles", "must be a mapping")
    role_names = set(raw)
    required_roles = set(DEFINED_ROLES)
    if role_names != required_roles:
        if role_names - required_roles:
            raise _config_error("roles", "contains an unknown role")
        raise _config_error("roles", "must define every required role")

    validated: dict[str, tuple[str, ...]] = {}
    for role in DEFINED_ROLES:
        role_config = raw[role]
        if not isinstance(role_config, dict):
            raise _config_error(f"roles.{role}", "must be a mapping")
        _reject_unknown_keys(
            role_config, {"permissions"}, section=f"roles.{role}"
        )
        permissions = role_config.get("permissions")
        if not isinstance(permissions, list) or not permissions:
            raise _config_error(
                f"roles.{role}.permissions", "must be a non-empty list"
            )
        if any(not isinstance(permission, str) for permission in permissions):
            raise _config_error(
                f"roles.{role}.permissions", "must contain only strings"
            )
        if len(set(permissions)) != len(permissions):
            raise _config_error(
                f"roles.{role}.permissions", "must not contain duplicates"
            )
        if not set(permissions).issubset(DEFINED_PERMISSIONS):
            raise _config_error(
                f"roles.{role}.permissions", "contains an unknown permission"
            )
        # Normalize policy file ordering to the stable machine vocabulary.
        validated[role] = tuple(
            permission
            for permission in DEFINED_PERMISSIONS
            if permission in permissions
        )

    if set(validated["admin"]) != set(DEFINED_PERMISSIONS):
        raise _config_error(
            "roles.admin.permissions", "must grant every defined permission"
        )
    return MappingProxyType(validated)


def _validate_policy_mapping(raw: Mapping[str, Any]) -> AccessControlPolicy:
    allowed = {
        "policy_id",
        "policy_version",
        "auth_method",
        "issuer",
        "audience",
        "algorithm",
        "max_token_ttl_seconds",
        "clock_skew_seconds",
        "roles",
    }
    _reject_unknown_keys(raw, allowed, section="top level")
    missing = allowed - set(raw)
    if missing:
        raise AccessControlConfigError(
            f"{_POLICY_NAME} policy: all required fields must be present."
        )

    policy_id = _required_string(raw, "policy_id", maximum=128)
    policy_version = _required_string(raw, "policy_version", maximum=64)
    auth_method = _required_string(raw, "auth_method", maximum=64)
    issuer = _required_string(raw, "issuer")
    audience = _required_string(raw, "audience")
    algorithm = _required_string(raw, "algorithm", maximum=32)
    if auth_method != AUTH_METHOD:
        raise _config_error("auth_method", f"must be exactly {AUTH_METHOD}")
    if algorithm != AUTH_ALGORITHM:
        raise _config_error("algorithm", f"must be exactly {AUTH_ALGORITHM}")

    max_ttl = _bounded_policy_int(
        raw,
        "max_token_ttl_seconds",
        minimum=MIN_MAX_TOKEN_TTL_SECONDS,
        maximum=MAX_MAX_TOKEN_TTL_SECONDS,
    )
    clock_skew = _bounded_policy_int(
        raw,
        "clock_skew_seconds",
        minimum=MIN_CLOCK_SKEW_SECONDS,
        maximum=MAX_CLOCK_SKEW_SECONDS,
    )
    role_permissions = _validate_role_permissions(raw["roles"])
    return AccessControlPolicy(
        policy_id=policy_id,
        policy_version=policy_version,
        auth_method=auth_method,
        issuer=issuer,
        audience=audience,
        algorithm=algorithm,
        max_token_ttl_seconds=max_ttl,
        clock_skew_seconds=clock_skew,
        role_permissions=role_permissions,
    )


def load_access_control_policy(
    path: str | Path | None = None,
) -> AccessControlPolicy:
    """Load the strict policy, using identical defaults only when absent.

    A present but malformed policy always raises ``AccessControlConfigError``.
    Error text includes neither an absolute path nor configured string values.
    """
    target = Path(path) if path is not None else _POLICY_PATH
    raw = _read_policy_mapping(target)
    if raw is None:
        raw = _DEFAULT_POLICY
    return _validate_policy_mapping(raw)


_PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "changeme",
    "changethissecret",
    "replacewith",
    "replacethis",
    "placeholder",
    "yourauthsecret",
    "yoursecrethere",
    "defaultsecret",
    "exampleauthsecret",
    "notarealsecret",
    "notasecuresecret",
    "insecuresecret",
    "temporarysecret",
    "developmentsecret",
    "samplesecret",
    "passwordpassword",
)


def _is_repeated_secret_pattern(value: str) -> bool:
    """Recognize obvious repeated placeholders without estimating entropy."""
    return value in (value + value)[1:-1]


def validate_auth_secret(value: str | None) -> str:
    """Validate the runtime HS256 secret without ever reproducing its value."""
    if value is None or not isinstance(value, str) or not value:
        raise AccessControlConfigError(
            f"{AUTH_SECRET_ENV} is required for comparison API authentication "
            "and local token issuance."
        )
    if value != value.strip() or _contains_control(value):
        raise AccessControlConfigError(
            f"{AUTH_SECRET_ENV} must not contain surrounding whitespace or "
            "control characters."
        )
    try:
        secret_bytes = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise AccessControlConfigError(
            f"{AUTH_SECRET_ENV} must be valid UTF-8 text."
        ) from None
    if secret_bytes < MIN_AUTH_SECRET_BYTES:
        raise AccessControlConfigError(
            f"{AUTH_SECRET_ENV} must contain at least "
            f"{MIN_AUTH_SECRET_BYTES} UTF-8 bytes."
        )
    if secret_bytes > MAX_AUTH_SECRET_BYTES:
        raise AccessControlConfigError(
            f"{AUTH_SECRET_ENV} must contain at most "
            f"{MAX_AUTH_SECRET_BYTES} UTF-8 bytes."
        )
    compact = re.sub(r"[^a-z0-9]", "", value.casefold())
    if (
        len(set(value)) < 8
        or _is_repeated_secret_pattern(value)
        or any(marker in compact for marker in _PLACEHOLDER_MARKERS)
    ):
        raise AccessControlConfigError(
            f"{AUTH_SECRET_ENV} must be a non-placeholder, high-entropy value."
        )
    return value


def _invalid_access_token() -> AccessTokenError:
    return AccessTokenError(
        INVALID_ACCESS_TOKEN,
        "The access token is invalid.",
    )


def _expired_access_token() -> AccessTokenError:
    return AccessTokenError(
        ACCESS_TOKEN_EXPIRED,
        "The access token is expired.",
    )


def _contains_control(value: str) -> bool:
    return any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in value
    )


def _validate_subject(value: Any, *, issuance: bool) -> str:
    error_type = TokenIssuanceError if issuance else None
    valid = (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= MAX_SUBJECT_CHARS
        and not _contains_control(value)
    )
    if not valid:
        if error_type is not None:
            raise error_type(
                f"subject must be a non-empty trimmed string of at most "
                f"{MAX_SUBJECT_CHARS} characters without control characters"
            )
        raise _invalid_access_token()
    return value


def _validate_token_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_TOKEN_ID_CHARS
        or _contains_control(value)
    ):
        raise _invalid_access_token()
    return value


def _validate_claim_roles(
    value: Any,
    policy: AccessControlPolicy,
    *,
    issuance: bool,
) -> tuple[str, ...]:
    if (
        not isinstance(value, (list, tuple))
        or not value
        or any(not isinstance(role, str) for role in value)
        or len(set(value)) != len(value)
        or not set(value).issubset(policy.role_permissions)
    ):
        if issuance:
            raise TokenIssuanceError(
                "roles must be a non-empty unique list of policy-defined roles"
            )
        raise _invalid_access_token()
    return tuple(sorted(value))


def _integer_numeric_date(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid_access_token()
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_now(now: datetime | None) -> datetime:
    value = now or _utc_now()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _to_utc_datetime(timestamp: int) -> datetime:
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise _invalid_access_token() from None


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class Authenticator:
    """Verify locally issued HS256 access tokens against one checked policy."""

    def __init__(self, policy: AccessControlPolicy, secret: str):
        self.policy = policy
        self.__secret = validate_auth_secret(secret)

    @classmethod
    def from_environment(
        cls,
        *,
        policy: AccessControlPolicy | None = None,
        policy_path: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "Authenticator":
        if policy is not None and policy_path is not None:
            raise AccessControlConfigError(
                "access_control configuration must supply policy or policy_path, "
                "not both."
            )
        loaded_policy = policy or load_access_control_policy(policy_path)
        source = os.environ if environ is None else environ
        return cls(policy=loaded_policy, secret=source.get(AUTH_SECRET_ENV))

    def verify(
        self,
        token: str,
        *,
        now: datetime | None = None,
    ) -> Principal:
        """Verify signature and claims, then build an allowlisted Principal.

        The payload is observed only after ``jwt.decode`` has authenticated the
        signature with the fixed HS256 algorithm and exact issuer/audience.
        Temporal semantics are then checked explicitly: expiry is strict,
        while configured skew applies only to future ``iat`` and ``nbf``.
        """
        if (
            not isinstance(token, str)
            or not token
            or len(token) > MAX_ACCESS_TOKEN_CHARS
        ):
            raise _invalid_access_token()

        try:
            payload = jwt.decode(
                token,
                self.__secret,
                algorithms=[self.policy.algorithm],
                issuer=self.policy.issuer,
                audience=self.policy.audience,
                options={
                    "require": [
                        "sub",
                        "roles",
                        "iss",
                        "aud",
                        "iat",
                        "nbf",
                        "exp",
                        "jti",
                        "typ",
                    ],
                    # Signature, issuer, and audience remain PyJWT-verified.
                    # NumericDate types and temporal boundaries are stricter
                    # below (integer-only; expiry gets no clock-skew grace).
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                    "strict_aud": True,
                },
            )
        except (jwt.PyJWTError, TypeError, ValueError):
            raise _invalid_access_token() from None

        # Redundant exact scalar checks make the contract independent of any
        # future library broadening (for example a sequence-valued audience).
        if (
            payload.get("iss") != self.policy.issuer
            or not isinstance(payload.get("iss"), str)
            or payload.get("aud") != self.policy.audience
            or not isinstance(payload.get("aud"), str)
            or payload.get("typ") != "access"
        ):
            raise _invalid_access_token()

        subject = _validate_subject(payload.get("sub"), issuance=False)
        roles = _validate_claim_roles(
            payload.get("roles"), self.policy, issuance=False
        )
        token_id = _validate_token_id(payload.get("jti"))
        issued_at = _integer_numeric_date(payload.get("iat"))
        not_before = _integer_numeric_date(payload.get("nbf"))
        expires_at = _integer_numeric_date(payload.get("exp"))
        current = _validate_now(now)
        current_timestamp = current.timestamp()

        # Structural claim defects remain generic invalid-token failures even
        # when the malformed exp also happens to be in the past.
        if expires_at <= issued_at:
            raise _invalid_access_token()
        if expires_at - issued_at > self.policy.max_token_ttl_seconds:
            raise _invalid_access_token()
        if not_before >= expires_at:
            raise _invalid_access_token()
        skew = self.policy.clock_skew_seconds
        if issued_at > current_timestamp + skew:
            raise _invalid_access_token()
        if not_before > current_timestamp + skew:
            raise _invalid_access_token()
        if expires_at <= current_timestamp:
            raise _expired_access_token()

        permissions = self.policy.permissions_for_roles(roles)
        return Principal(
            subject=subject,
            roles=roles,
            permissions=permissions,
            token_id=token_id,
            issued_at=_to_utc_datetime(issued_at),
            expires_at=_to_utc_datetime(expires_at),
            auth_method=self.policy.auth_method,
            policy_id=self.policy.policy_id,
            policy_version=self.policy.policy_version,
        )


def issue_access_token(
    *,
    policy: AccessControlPolicy,
    secret: str,
    subject: str,
    roles: Sequence[str],
    ttl_seconds: int,
    now: datetime | None = None,
) -> str:
    """Issue one signed local access token; no file or network is touched."""
    validated_secret = validate_auth_secret(secret)
    clean_subject = _validate_subject(subject, issuance=True)
    clean_roles = _validate_claim_roles(roles, policy, issuance=True)
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise TokenIssuanceError("ttl-seconds must be an integer")
    if ttl_seconds < 1 or ttl_seconds > policy.max_token_ttl_seconds:
        raise TokenIssuanceError(
            f"ttl-seconds must be between 1 and "
            f"{policy.max_token_ttl_seconds}"
        )
    issued = _validate_now(now)
    issued_timestamp = int(issued.timestamp())
    payload = {
        "sub": clean_subject,
        "roles": list(clean_roles),
        "iss": policy.issuer,
        "aud": policy.audience,
        "iat": issued_timestamp,
        "nbf": issued_timestamp,
        "exp": issued_timestamp + ttl_seconds,
        "jti": secrets.token_urlsafe(24),
        "typ": "access",
    }
    try:
        token = jwt.encode(
            payload,
            validated_secret,
            algorithm=policy.algorithm,
            headers={"typ": "JWT"},
        )
    except (jwt.PyJWTError, TypeError, ValueError):
        raise TokenIssuanceError("access token could not be issued") from None
    if not isinstance(token, str):
        raise TokenIssuanceError("access token could not be issued")
    return token


# Policy validation is fail-fast at import.  Secret validation is intentionally
# deferred until API construction or explicit local issuance.
POLICY = load_access_control_policy()
