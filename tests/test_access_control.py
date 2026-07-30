"""Authentication, authorization, attribution, migration, and CLI coverage.

Every test is local and offline. Tokens are generated in memory with the same
PyJWT-backed issuer used by the CLI; no AWS, model, network, user database, or
token file is involved.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import jwt
import pytest
import yaml
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import access_control
import api
import comparison_reliability
import comparison_store
import config
import detection_recovery
from tests.auth_helpers import authorization_headers, issue_test_access_token


REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO_ROOT / "policies" / "access_control_policy.yaml"
CLI_PATH = REPO_ROOT / "scripts" / "issue_local_access_token.py"
TEST_SECRET = os.environ[access_control.AUTH_SECRET_ENV]
POLICY = access_control.load_access_control_policy()
AUTHENTICATOR = access_control.Authenticator(policy=POLICY, secret=TEST_SECRET)
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _claims(**overrides):
    issued_at = int(NOW.timestamp())
    claims = {
        "sub": "actor@example.local",
        "roles": ["viewer"],
        "iss": POLICY.issuer,
        "aud": POLICY.audience,
        "iat": issued_at,
        "nbf": issued_at,
        "exp": issued_at + 300,
        "jti": "jti_access_control_unit_01",
        "typ": "access",
    }
    claims.update(overrides)
    return claims


def _encode(
    claims=None,
    *,
    secret: str = TEST_SECRET,
    algorithm: str = "HS256",
) -> str:
    return jwt.encode(
        claims or _claims(),
        secret,
        algorithm=algorithm,
        headers={"typ": "JWT"},
    )


def _assert_invalid(token: str, code: str = access_control.INVALID_ACCESS_TOKEN):
    with pytest.raises(access_control.AccessTokenError) as excinfo:
        AUTHENTICATOR.verify(token, now=NOW)
    assert excinfo.value.code == code
    assert token not in str(excinfo.value)
    assert TEST_SECRET not in str(excinfo.value)


def _policy_document() -> dict:
    return yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))


def _write_policy(path: Path, document: dict) -> Path:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _role_client(role: str, *, subject: str | None = None) -> TestClient:
    return TestClient(
        api.app,
        headers=authorization_headers(
            subject=subject or f"{role}@example.local", roles=(role,)
        ),
    )


def _assert_marker_absent_from_log_records(
    marker: str, records: list[logging.LogRecord]
) -> None:
    """Inspect every LogRecord value, not only its rendered message."""
    for record in records:
        for value in vars(record).values():
            assert marker not in repr(value)


# --- Policy and secret configuration -----------------------------------------


def test_checked_policy_and_missing_file_default_are_identical(tmp_path):
    checked = access_control.load_access_control_policy(POLICY_PATH)
    missing = access_control.load_access_control_policy(tmp_path / "absent.yaml")
    assert checked == missing
    assert tuple(checked.role_permissions) == access_control.DEFINED_ROLES
    assert set(checked.role_permissions["admin"]) == set(
        access_control.DEFINED_PERMISSIONS
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: document.update({"unknown_top_level": True}),
        lambda document: document["roles"].update(
            {"unknown_role": {"permissions": ["comparison.read"]}}
        ),
        lambda document: document["roles"]["viewer"]["permissions"].append(
            "unknown.permission"
        ),
        lambda document: document["roles"]["viewer"]["permissions"].append(
            "comparison.read"
        ),
        lambda document: document.update({"max_token_ttl_seconds": True}),
        lambda document: document.update({"clock_skew_seconds": 301}),
        lambda document: document["roles"].pop("admin"),
    ],
)
def test_invalid_policy_shapes_fail_closed(tmp_path, mutate):
    document = _policy_document()
    mutate(document)
    path = _write_policy(tmp_path / "policy.yaml", document)
    with pytest.raises(access_control.AccessControlConfigError) as excinfo:
        access_control.load_access_control_policy(path)
    text = str(excinfo.value)
    assert str(tmp_path) not in text
    assert TEST_SECRET not in text


def test_duplicate_yaml_role_key_is_rejected(tmp_path):
    text = POLICY_PATH.read_text(encoding="utf-8")
    text += "\nroles:\n  viewer:\n    permissions: [comparison.read]\n"
    path = tmp_path / "duplicate.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(access_control.AccessControlConfigError, match="duplicate"):
        access_control.load_access_control_policy(path)


def test_present_policy_with_invalid_utf8_fails_closed_and_sanitized(tmp_path):
    path = tmp_path / "invalid-utf8-policy.yaml"
    path.write_bytes(b"\xff\xfe\xfa")
    with pytest.raises(access_control.AccessControlConfigError) as excinfo:
        access_control.load_access_control_policy(path)
    message = str(excinfo.value)
    assert str(tmp_path) not in message
    assert TEST_SECRET not in message
    assert "invalid-utf8-policy.yaml" in message


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "short",
        "x" * 64,
        "change-me-placeholder-secret-value-123456789",
        " valid-secret-with-leading-space-1234567890",
        "valid-secret-with-control-1234567890\n",
        "valid-looking-secret-with-surrogate-\ud800-1234567890",
    ],
)
def test_missing_weak_default_or_placeholder_secret_fails_closed(value):
    with pytest.raises(access_control.AccessControlConfigError) as excinfo:
        access_control.validate_auth_secret(value)
    if value:
        assert value not in str(excinfo.value)


def test_valid_secret_is_accepted_without_serialization():
    assert access_control.validate_auth_secret(TEST_SECRET) == TEST_SECRET
    authenticator = access_control.Authenticator(policy=POLICY, secret=TEST_SECRET)
    assert not hasattr(authenticator, "secret")
    assert TEST_SECRET not in repr(authenticator)


# --- Token verification and Principal ----------------------------------------


def test_valid_token_resolves_allowlisted_immutable_principal():
    token = access_control.issue_access_token(
        policy=POLICY,
        secret=TEST_SECRET,
        subject="alice@example.local",
        roles=("reviewer", "operator"),
        ttl_seconds=300,
        now=NOW,
    )
    principal = AUTHENTICATOR.verify(token, now=NOW)
    assert principal.subject == "alice@example.local"
    assert principal.roles == ("operator", "reviewer")
    assert set(principal.permissions) == (
        set(POLICY.role_permissions["operator"])
        | set(POLICY.role_permissions["reviewer"])
    )
    assert principal.auth_method == "local_hs256"
    assert principal.policy_id == POLICY.policy_id
    assert principal.policy_version == POLICY.policy_version
    assert token not in principal.to_dict().values()
    assert set(principal.to_dict()) == {
        "subject",
        "roles",
        "permissions",
        "token_id",
        "issued_at",
        "expires_at",
        "auth_method",
        "policy_id",
        "policy_version",
    }
    with pytest.raises(Exception):
        principal.subject = "mallory"  # type: ignore[misc]


def test_invalid_signature_is_rejected():
    _assert_invalid(_encode(secret="different-valid-secret-material-1234567890"))


def test_alg_none_is_rejected():
    token = jwt.encode(_claims(), key="", algorithm="none")
    _assert_invalid(token)


def test_wrong_algorithm_is_rejected():
    _assert_invalid(_encode(algorithm="HS384"))


@pytest.mark.parametrize(
    "claim,value",
    [
        ("iss", "wrong-issuer"),
        ("aud", "wrong-audience"),
        ("aud", [POLICY.audience]),
        ("typ", "refresh"),
    ],
)
def test_exact_issuer_audience_and_access_type_are_required(claim, value):
    _assert_invalid(_encode(_claims(**{claim: value})))


@pytest.mark.parametrize(
    "missing",
    ["sub", "roles", "iss", "aud", "iat", "nbf", "exp", "jti", "typ"],
)
def test_every_required_claim_is_enforced(missing):
    claims = _claims()
    claims.pop(missing)
    _assert_invalid(_encode(claims))


def test_expired_token_has_stable_expired_contract():
    issued = int(NOW.timestamp()) - 100
    token = _encode(_claims(iat=issued, nbf=issued, exp=int(NOW.timestamp()) - 1))
    _assert_invalid(token, access_control.ACCESS_TOKEN_EXPIRED)


def test_future_nbf_and_iat_beyond_skew_are_rejected():
    future = int(NOW.timestamp()) + POLICY.clock_skew_seconds + 1
    _assert_invalid(_encode(_claims(nbf=future)))
    _assert_invalid(_encode(_claims(iat=future, exp=future + 100)))


def test_future_nbf_and_iat_at_skew_boundary_are_accepted():
    future = int(NOW.timestamp()) + POLICY.clock_skew_seconds
    token = _encode(_claims(iat=future, nbf=future, exp=future + 100))
    assert AUTHENTICATOR.verify(token, now=NOW).subject == "actor@example.local"


def test_exp_must_follow_iat_and_ttl_is_bounded():
    timestamp = int(NOW.timestamp())
    _assert_invalid(_encode(_claims(iat=timestamp + 60, exp=timestamp + 60)))
    _assert_invalid(
        _encode(
            _claims(
                iat=timestamp,
                exp=timestamp + POLICY.max_token_ttl_seconds + 1,
            )
        )
    )


@pytest.mark.parametrize("claim", ["iat", "nbf", "exp"])
@pytest.mark.parametrize("value", [True, 1.5, "123"])
def test_numeric_dates_are_integer_and_never_bool(claim, value):
    _assert_invalid(_encode(_claims(**{claim: value})))


@pytest.mark.parametrize(
    "sub",
    [
        "",
        " actor",
        "actor\nname",
        "actor\ud800name",
        "x" * (access_control.MAX_SUBJECT_CHARS + 1),
    ],
)
def test_empty_malformed_or_control_character_subject_is_rejected(sub):
    _assert_invalid(_encode(_claims(sub=sub)))


@pytest.mark.parametrize(
    "roles",
    [
        [],
        ["unknown"],
        ["viewer", "viewer"],
        "viewer",
        ["viewer", 7],
    ],
)
def test_roles_must_be_nonempty_unique_and_allowlisted(roles):
    _assert_invalid(_encode(_claims(roles=roles)))


@pytest.mark.parametrize("jti", ["", "bad\njti", "bad\ud800jti", "x" * 129, 7])
def test_token_id_is_nonempty_bounded_and_safe(jti):
    _assert_invalid(_encode(_claims(jti=jti)))


def test_unknown_permission_is_never_granted_and_multi_role_is_union():
    principal = AUTHENTICATOR.verify(
        _encode(_claims(roles=["operator", "reviewer"])), now=NOW
    )
    assert "recovery.replay" in principal.permissions
    assert "review.decide" in principal.permissions
    assert "export.create" not in principal.permissions
    assert "comparison.*" not in principal.permissions
    assert "unknown.permission" not in principal.permissions


# --- API 401/403 and exact route policy --------------------------------------


def test_missing_invalid_and_expired_credentials_return_sanitized_401(caplog):
    client = TestClient(api.app)
    with caplog.at_level(logging.WARNING, logger="api"):
        missing = client.get("/api/comparisons")
        invalid_value = "header.payload.signature-SENSITIVE-MARKER"
        invalid = client.get(
            "/api/comparisons",
            headers={"Authorization": f"Bearer {invalid_value}"},
        )
        issued = int(NOW.timestamp()) - 100
        expired_token = _encode(
            _claims(iat=issued, nbf=issued, exp=int(NOW.timestamp()) - 1)
        )
        expired = client.get(
            "/api/comparisons",
            headers={"Authorization": f"Bearer {expired_token}"},
        )

    assert missing.status_code == invalid.status_code == expired.status_code == 401
    assert missing.json()["detail"]["code"] == "authentication_required"
    assert invalid.json()["detail"]["code"] == "invalid_access_token"
    assert expired.json()["detail"]["code"] == "access_token_expired"
    for response in (missing, invalid, expired):
        assert response.headers["www-authenticate"] == "Bearer"
        detail = response.json()["detail"]
        assert set(detail) == {"code", "message", "error_id"}
        assert detail["error_id"].startswith("err_")
        assert TEST_SECRET not in response.text
    assert invalid_value not in caplog.text
    assert expired_token not in caplog.text
    assert TEST_SECRET not in caplog.text
    for record in caplog.records:
        assert not hasattr(record, "authorization")


def test_missing_credentials_precede_malformed_json_parsing(caplog):
    client = TestClient(api.app)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="api"):
        response = client.post(
            "/api/comparisons",
            content=b'{"previousFilingId": "unterminated"',
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"]["code"] == "authentication_required"
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "authentication_rejected"
    ]
    assert len(records) == 1
    assert records[0].route == "/api/comparisons"
    assert records[0].required_permission == "comparison.create"
    assert not any(
        getattr(record, "event", None) == "protected_request_validation_rejected"
        for record in caplog.records
    )


def test_insufficient_permission_precedes_malformed_json_parsing(caplog):
    viewer = _role_client("viewer")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="api"):
        response = viewer.post(
            "/api/comparisons",
            content=b'{"previousFilingId": "unterminated"',
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "insufficient_permission"
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "authorization_rejected"
    ]
    assert len(records) == 1
    assert records[0].route == "/api/comparisons"
    assert records[0].required_permission == "comparison.create"
    assert not any(
        getattr(record, "event", None) == "protected_request_validation_rejected"
        for record in caplog.records
    )


def test_authorized_invalid_body_is_sanitized_without_token_in_logs(
    caplog, monkeypatch
):
    def route_must_not_run(*args, **kwargs):
        raise AssertionError("validation failure reached comparison mutation")

    monkeypatch.setattr(api.comparison_store, "create_comparison", route_must_not_run)
    operator = _role_client("operator")
    body_token = issue_test_access_token(
        subject="body-token@example.local",
        roles=("viewer",),
        ttl_seconds=300,
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="api"):
        response = operator.post(
            "/api/comparisons",
            json={
                "previousFilingId": "previous",
                "currentFilingId": "current",
                "sectionScope": {
                    "Authorization": f"Bearer {body_token}",
                },
            },
        )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert set(detail) == {"code", "message", "error_id"}
    assert detail["code"] == "invalid_request"
    assert detail["error_id"].startswith("err_")
    assert body_token not in response.text
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None)
        == "protected_request_validation_rejected"
    ]
    assert len(records) == 1
    assert records[0].route == "/api/comparisons"
    assert records[0].required_permission == "comparison.create"
    assert body_token not in caplog.text
    _assert_marker_absent_from_log_records(body_token, caplog.records)


def test_dynamic_path_containing_complete_token_is_never_logged(caplog):
    path_token = issue_test_access_token(
        subject="path-token@example.local",
        roles=("viewer",),
        ttl_seconds=300,
    )
    client = TestClient(api.app)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="api"):
        response = client.post(
            f"/api/comparisons/{path_token}/detect",
            content=b"{",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 401
    assert path_token not in response.text
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "authentication_rejected"
    ]
    assert len(records) == 1
    assert records[0].route == "/api/comparisons/{comparison_id}/detect"
    assert records[0].required_permission == "comparison.detect"
    assert path_token not in caplog.text
    _assert_marker_absent_from_log_records(path_token, caplog.records)


def test_authenticated_compact_token_inputs_never_reach_domain_or_response(
    caplog, monkeypatch
):
    def domain_must_not_run(*args, **kwargs):
        raise AssertionError("compact token input reached domain code")

    monkeypatch.setattr(api.comparison_store, "create_comparison", domain_must_not_run)
    monkeypatch.setattr(api.comparison_store, "get_comparison", domain_must_not_run)
    monkeypatch.setattr(api.detection_recovery, "replay_attempt", domain_must_not_run)
    monkeypatch.setattr(api.comparison_review, "decide", domain_must_not_run)
    monkeypatch.setattr(
        api.comparison_governance, "govern", domain_must_not_run
    )
    monkeypatch.setattr(
        api.comparison_export, "export_comparison", domain_must_not_run
    )

    injected_token = issue_test_access_token(
        subject="must-not-be-reflected@example.local",
        roles=("viewer",),
        ttl_seconds=300,
    )
    cases = (
        (
            _role_client("operator"),
            "POST",
            "/api/comparisons",
            {
                "previousFilingId": "previous",
                "currentFilingId": "current",
                "sectionScope": [injected_token],
            },
        ),
        (
            _role_client("operator"),
            "POST",
            "/api/detection-attempts/att_missing/replay",
            {
                "reasonCode": "operator_replay_stale_attempt",
                "operatorNote": f"do not retain {injected_token}",
            },
        ),
        (
            _role_client("reviewer"),
            "POST",
            "/api/comparison-reviews/crev_missing/decision",
            {
                "action": "approved",
                "reasonCode": "approved_as_is",
                "reviewerNote": injected_token,
            },
        ),
        (
            _role_client("exporter"),
            "POST",
            "/api/comparisons/cmp_missing/exports",
            {"evaluationId": injected_token},
        ),
        (
            _role_client("operator"),
            "POST",
            f"/api/comparisons/{injected_token}/governance",
            None,
        ),
        (
            _role_client("viewer"),
            "GET",
            f"/api/comparisons/{injected_token}",
            None,
        ),
    )

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="api"):
        for client, method, path, body in cases:
            kwargs = {} if body is None else {"json": body}
            response = client.request(method, path, **kwargs)
            assert response.status_code == 422
            assert response.json()["detail"]["code"] == "invalid_request"
            assert injected_token not in response.text

    assert injected_token not in caplog.text
    _assert_marker_absent_from_log_records(injected_token, caplog.records)
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None)
        == "protected_request_validation_rejected"
    ]
    assert len(records) == len(cases)
    assert all(record.route.startswith("/api/") for record in records)
    assert all(record.required_permission for record in records)


def test_valid_viewer_can_read_but_cannot_mutate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "COMPARISON_DB_PATH", str(tmp_path / "db.sqlite"))
    viewer = _role_client("viewer")
    assert viewer.get("/api/comparisons").status_code == 200
    for method, path, body in (
        ("post", "/api/comparisons", {}),
        ("post", "/api/comparisons/cmp_missing/detect", {}),
        (
            "post",
            "/api/detection-attempts/att_missing/replay",
            {"reasonCode": "operator_replay_stale_attempt", "operatorNote": "note"},
        ),
        (
            "post",
            "/api/comparison-reviews/crev_missing/decision",
            {
                "action": "approved",
                "reasonCode": "approved_as_is",
                "reviewerNote": "note",
            },
        ),
        (
            "post",
            "/api/comparisons/cmp_missing/exports",
            {"evaluationId": "gov_missing"},
        ),
    ):
        response = getattr(viewer, method)(path, json=body)
        assert response.status_code == 403, (path, response.text)
        detail = response.json()["detail"]
        assert detail["code"] == "insufficient_permission"
        assert set(detail) == {"code", "message", "error_id"}


def test_role_permissions_match_the_explicit_policy():
    viewer = set(POLICY.role_permissions["viewer"])
    assert viewer == {
        "comparison.read",
        "detection_attempt.read",
        "recovery.read",
        "reliability.read",
        "governance.read",
        "review.read",
        "export.read",
    }
    assert set(POLICY.role_permissions["operator"]) == viewer | {
        "comparison.create",
        "comparison.detect",
        "recovery.replay",
        "governance.evaluate",
    }
    assert set(POLICY.role_permissions["reviewer"]) == viewer | {"review.decide"}
    assert set(POLICY.role_permissions["exporter"]) == viewer | {"export.create"}
    assert set(POLICY.role_permissions["admin"]) == set(
        access_control.DEFINED_PERMISSIONS
    )


@pytest.mark.parametrize(
    "role,forbidden_paths",
    [
        (
            "operator",
            (
                "/api/comparison-reviews/crev_x/decision",
                "/api/comparisons/cmp_x/exports",
            ),
        ),
        (
            "reviewer",
            (
                "/api/detection-attempts/att_x/replay",
                "/api/comparisons/cmp_x/exports",
            ),
        ),
        (
            "exporter",
            (
                "/api/detection-attempts/att_x/replay",
                "/api/comparison-reviews/crev_x/decision",
            ),
        ),
    ],
)
def test_specialized_roles_cannot_cross_mutation_boundaries(role, forbidden_paths):
    client = _role_client(role)
    bodies = {
        "/api/comparison-reviews/crev_x/decision": {
            "action": "approved",
            "reasonCode": "approved_as_is",
            "reviewerNote": "note",
        },
        "/api/comparisons/cmp_x/exports": {"evaluationId": "gov_x"},
        "/api/detection-attempts/att_x/replay": {
            "reasonCode": "operator_replay_stale_attempt",
            "operatorNote": "note",
        },
    }
    for path in forbidden_paths:
        assert client.post(path, json=bodies[path]).status_code == 403


def test_authentication_and_authorization_run_before_lookup_or_mutation(monkeypatch):
    calls = []

    def forbidden_call(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("route work ran before authentication")

    monkeypatch.setattr(api.comparison_store, "get_comparison", forbidden_call)
    assert TestClient(api.app).get("/api/comparisons/cmp_secret").status_code == 401
    assert calls == []

    monkeypatch.setattr(api.comparison_store, "create_comparison", forbidden_call)
    viewer = _role_client("viewer")
    response = viewer.post(
        "/api/comparisons",
        json={
            "previousFilingId": "a",
            "currentFilingId": "b",
        },
    )
    assert response.status_code == 403
    assert calls == []


def test_operator_can_create_and_detect(monkeypatch):
    created_record = {
        "comparison_id": "cmp_operator_allowed",
        "schema_version": comparison_store.COMPARISON_SCHEMA_VERSION,
        "workflow_version": comparison_store.WORKFLOW_VERSION,
        "previous_filing_id": "filing_previous",
        "current_filing_id": "filing_current",
        "section_scope": ["item_1a_risk_factors"],
        "status": "ready_for_detection",
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "failure_code": None,
        "failure_summary": None,
    }
    captured = {}

    def fake_create(*args, **kwargs):
        captured["create"] = (args, kwargs)
        return created_record, True

    def fake_enqueue(comparison_id, **kwargs):
        captured["detect"] = (comparison_id, kwargs)
        return {
            "kind": "job",
            "created": True,
            "job": {
                "job_id": "djob_operator_allowed",
                "comparison_id": comparison_id,
                "attempt_id": None,
                "status": "queued",
                "queued_at": NOW.isoformat(),
            },
        }

    monkeypatch.setattr(api.comparison_store, "create_comparison", fake_create)
    monkeypatch.setattr(
        api.comparison_detection_worker,
        "enqueue_initial_detection",
        fake_enqueue,
    )
    client = _role_client("operator", subject="operator-positive@example.local")

    created = client.post(
        "/api/comparisons",
        json={
            "previousFilingId": "filing_previous",
            "currentFilingId": "filing_current",
            "sectionScope": ["item_1a_risk_factors"],
        },
    )
    detected = client.post("/api/comparisons/cmp_operator_allowed/detect")

    assert created.status_code == 201
    assert created.json()["comparison"]["comparisonId"] == "cmp_operator_allowed"
    assert detected.status_code == 202
    assert detected.json()["jobId"] == "djob_operator_allowed"
    assert detected.json()["attemptId"] is None
    assert captured["create"][0][:2] == ("filing_previous", "filing_current")
    assert (
        captured["detect"][1]["actor_context"]["actor_subject"]
        == "operator-positive@example.local"
    )
    assert (
        captured["detect"][1]["actor_context"]["required_permission"]
        == "comparison.detect"
    )


def test_exporter_can_create_export(monkeypatch):
    captured = {}

    def fake_export(comparison_id, evaluation_id):
        captured["args"] = (comparison_id, evaluation_id)
        return {
            "export": {
                "export_schema_version": "comparison.export.v1",
                "export_id": "exp_exporter_allowed",
            }
        }, True

    monkeypatch.setattr(
        api.comparison_export, "export_comparison", fake_export
    )
    client = _role_client("exporter")
    response = client.post(
        "/api/comparisons/cmp_exporter_allowed/exports",
        json={"evaluationId": "gov_exporter_allowed"},
    )

    assert response.status_code == 201
    assert response.json()["export"]["export_id"] == "exp_exporter_allowed"
    assert captured["args"] == (
        "cmp_exporter_allowed",
        "gov_exporter_allowed",
    )


def test_admin_reaches_every_comparison_route_without_auth_refusal(
    tmp_path, monkeypatch
):
    db = tmp_path / "admin-route-matrix.db"
    monkeypatch.setattr(config, "COMPARISON_DB_PATH", str(db))
    monkeypatch.setattr(
        config, "FILING_REGISTRY_PATH", str(tmp_path / "absent-registry.jsonl")
    )
    comparison_store.init_db(db)
    client = _role_client("admin")
    bodies = {
        ("POST", "/api/comparisons"): {
            "previousFilingId": "missing-previous",
            "currentFilingId": "missing-current",
        },
        (
            "POST",
            "/api/detection-attempts/{attempt_id}/replay",
        ): {
            "reasonCode": "operator_replay_stale_attempt",
            "operatorNote": "admin matrix check",
        },
        (
            "POST",
            "/api/comparison-reviews/{review_id}/decision",
        ): {
            "action": "approved",
            "reasonCode": "approved_as_is",
            "reviewerNote": "admin matrix check",
        },
        (
            "POST",
            "/api/comparisons/{comparison_id}/exports",
        ): {"evaluationId": "gov_missing"},
    }

    assert len(api.COMPARISON_ROUTE_PERMISSION_MATRIX) == 26
    for method, template in sorted(api.COMPARISON_ROUTE_PERMISSION_MATRIX):
        path = (
            template.replace("{comparison_id}", "cmp_missing")
            .replace("{attempt_id}", "att_missing")
            .replace("{review_id}", "crev_missing")
            .replace("{export_id}", "exp_missing")
        )
        kwargs = {}
        body = bodies.get((method, template))
        if body is not None:
            kwargs["json"] = body
        response = client.request(method, path, **kwargs)
        assert response.status_code not in {401, 403}, (
            method,
            template,
            response.text,
        )
        assert response.status_code < 500, (
            method,
            template,
            response.text,
        )


def test_rejected_mutations_leave_all_workflow_tables_unchanged(
    tmp_path, monkeypatch
):
    db = tmp_path / "rejected-mutations.db"
    monkeypatch.setattr(config, "COMPARISON_DB_PATH", str(db))
    comparison_store.init_db(db)
    tables = (
        "comparison_detection_attempts",
        "comparison_detection_replays",
        "comparison_review_events",
        "comparison_exports",
    )

    def counts():
        with closing(sqlite3.connect(db)) as conn:
            return {
                table: conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in tables
            }

    requests = (
        ("POST", "/api/comparisons/cmp_missing/detect", None),
        (
            "POST",
            "/api/detection-attempts/att_missing/replay",
            {
                "reasonCode": "operator_replay_stale_attempt",
                "operatorNote": "must not persist",
            },
        ),
        (
            "POST",
            "/api/comparison-reviews/crev_missing/decision",
            {
                "action": "approved",
                "reasonCode": "approved_as_is",
                "reviewerNote": "must not persist",
            },
        ),
        (
            "POST",
            "/api/comparisons/cmp_missing/exports",
            {"evaluationId": "gov_missing"},
        ),
    )
    before = counts()
    assert before == {table: 0 for table in tables}

    unauthenticated = TestClient(api.app)
    viewer = _role_client("viewer")
    for method, path, body in requests:
        kwargs = {} if body is None else {"json": body}
        assert unauthenticated.request(method, path, **kwargs).status_code == 401
        assert viewer.request(method, path, **kwargs).status_code == 403

    assert counts() == before


def _declared_permissions(route: APIRoute) -> set[str]:
    found: set[str] = set()
    pending = [route.dependant]
    while pending:
        dependant = pending.pop()
        permission = getattr(dependant.call, "required_permission", None)
        if permission:
            found.add(permission)
        pending.extend(dependant.dependencies)
    return found


def test_every_comparison_route_is_in_the_matrix_with_its_dependency():
    prefixes = (
        "/api/comparisons",
        "/api/detection-attempts",
        "/api/comparison-reliability",
        "/api/comparison-detection-jobs",
        "/api/comparison-reviews",
        "/api/comparison-exports",
    )
    actual: dict[tuple[str, str], APIRoute] = {}
    for route in api.app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith(prefixes):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            actual[(method, route.path)] = route

    assert set(actual) == set(api.COMPARISON_ROUTE_PERMISSION_MATRIX)
    for key, expected in api.COMPARISON_ROUTE_PERMISSION_MATRIX.items():
        declared = _declared_permissions(actual[key])
        assert declared == {expected}, (key, declared, expected)

    mutation_methods = {
        key
        for key in actual
        if key[0] in {"POST", "PUT", "PATCH", "DELETE"}
    }
    assert mutation_methods
    assert all(_declared_permissions(actual[key]) for key in mutation_methods)


def test_health_docs_and_generic_rag_surface_remain_public():
    client = TestClient(api.app)
    assert client.get("/api/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    # Invalid generic chat input reaches its existing route contract, not auth.
    response = client.post("/api/chat", json={"message": "   ", "history": []})
    assert response.status_code == 400
    assert response.json()["detail"] == "Message cannot be empty."


# --- Principal-derived replay/review actors ----------------------------------


def test_replay_actor_is_principal_and_legacy_field_must_match(monkeypatch):
    captured = []
    subject = "operator@example.local"

    def fake_replay(attempt_id, **kwargs):
        captured.append((attempt_id, kwargs))
        replay = {
            "replay_id": "rpl_unit",
            "comparison_id": "cmp_unit",
            "source_attempt_id": attempt_id,
            "replacement_attempt_id": "att_new",
            "operator_id": kwargs["operator_id"],
            "actor_auth_method": kwargs["actor_context"]["actor_auth_method"],
            "reason_code": kwargs["reason_code"],
            "operator_note": kwargs["operator_note"],
            "policy_id": "detection_recovery_v1",
            "policy_version": "1",
            "requested_at": NOW.isoformat(),
        }
        return {
            "replay": replay,
            "source_attempt_id": attempt_id,
            "replacement_attempt_id": "att_new",
            "replacement_status": "succeeded",
            "result": None,
        }, True

    monkeypatch.setattr(api.detection_recovery, "replay_attempt", fake_replay)
    client = _role_client("operator", subject=subject)
    body = {
        "reasonCode": "operator_replay_stale_attempt",
        "operatorNote": "replacement requested",
    }
    response = client.post("/api/detection-attempts/att_old/replay", json=body)
    assert response.status_code == 201
    assert response.json()["replay"]["operatorId"] == subject
    assert response.json()["replay"]["operatorIdBasis"] == "local_hs256"
    kwargs = captured[-1][1]
    assert kwargs["operator_id"] == subject
    assert kwargs["actor_context"]["actor_subject"] == subject
    assert kwargs["actor_context"]["required_permission"] == "recovery.replay"
    assert kwargs["actor_policy_id"] == POLICY.policy_id
    assert kwargs["actor_policy_version"] == POLICY.policy_version

    matching = client.post(
        "/api/detection-attempts/att_old/replay",
        json={**body, "operatorId": subject},
    )
    assert matching.status_code == 201

    before = len(captured)
    mismatch = client.post(
        "/api/detection-attempts/att_old/replay",
        json={**body, "operatorId": "impersonated@example.local"},
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["detail"]["code"] == "actor_identity_mismatch"
    assert len(captured) == before


def test_review_actor_is_principal_for_approval_and_rejection(monkeypatch):
    captured = []

    def fake_decide(review_id, **kwargs):
        captured.append((review_id, kwargs))
        return {
            "event_id": f"rev_evt_{len(captured)}",
            "review_id": review_id,
            "comparison_id": "cmp_unit",
            "evaluation_id": "gov_unit",
            "action": kwargs["action"],
            "reviewer_id": kwargs["reviewer_id"],
            "actor_auth_method": kwargs["actor_context"]["actor_auth_method"],
            "reason_code": kwargs["reason_code"],
            "reviewer_note": kwargs["reviewer_note"],
            "original_governed_result_hash": "a" * 64,
            "final_reviewed_result_hash": "b" * 64,
            "edits": [],
            "created_at": NOW.isoformat(),
            "reviewed_result": {},
        }, True

    monkeypatch.setattr(api.comparison_review, "decide", fake_decide)
    subject = "reviewer@example.local"
    client = _role_client("reviewer", subject=subject)
    for action, reason in (
        ("approved", "approved_as_is"),
        ("rejected", "rejected_other"),
    ):
        response = client.post(
            f"/api/comparison-reviews/crev_{action}/decision",
            json={
                "action": action,
                "reasonCode": reason,
                "reviewerNote": "checked",
            },
        )
        assert response.status_code == 201
        decision = response.json()["decision"]
        assert decision["reviewerId"] == subject
        assert decision["reviewerIdBasis"] == "local_hs256"
        assert captured[-1][1]["reviewer_id"] == subject
        assert (
            captured[-1][1]["actor_context"]["required_permission"]
            == "review.decide"
        )

    before = len(captured)
    mismatch = client.post(
        "/api/comparison-reviews/crev_x/decision",
        json={
            "action": "approved",
            "reviewerId": "other@example.local",
            "reasonCode": "approved_as_is",
            "reviewerNote": "checked",
        },
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["detail"]["code"] == "actor_identity_mismatch"
    assert len(captured) == before


# --- Durable attribution and migration ---------------------------------------


def _seed_running_attempt(db_path: Path) -> tuple[str, str]:
    comparison_store.init_db(db_path)
    comparison_id = "cmp_auth_storage"
    attempt_id = "att_auth_storage"
    started = (NOW - timedelta(hours=1)).isoformat()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            INSERT INTO comparisons (
                comparison_id, schema_version, workflow_version,
                previous_filing_id, current_filing_id, section_scope, status,
                created_at, updated_at, failure_code, failure_summary
            ) VALUES (?, ?, ?, ?, ?, ?, 'detecting', ?, ?, NULL, NULL)
            """,
            (
                comparison_id,
                comparison_store.COMPARISON_SCHEMA_VERSION,
                comparison_store.WORKFLOW_VERSION,
                "previous",
                "current",
                '["item_1a_risk_factors"]',
                started,
                started,
            ),
        )
        conn.execute(
            """
            INSERT INTO comparison_detection_attempts (
                attempt_id, comparison_id, attempt_number, status,
                detector_version, workflow_version,
                previous_source_hash, current_source_hash, started_at,
                finished_at, result_hash, failure_code, failure_summary
            ) VALUES (?, ?, 1, 'running', ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)
            """,
            (
                attempt_id,
                comparison_id,
                "detector-auth-test",
                comparison_store.WORKFLOW_VERSION,
                "prev-hash",
                "curr-hash",
                started,
            ),
        )
    return comparison_id, attempt_id


def test_authenticated_replay_persists_narrow_attribution_and_subject_idempotency(
    tmp_path,
):
    db = tmp_path / "replay.db"
    comparison_id, source_id = _seed_running_attempt(db)
    subject = "operator@example.local"
    request_hash = detection_recovery.replay_request_hash(
        source_attempt_id=source_id,
        operator_id=subject,
        reason_code="operator_replay_stale_attempt",
        operator_note="process ended",
        policy_id="detection_recovery_v1",
        policy_version="1",
    )
    row, created = comparison_store.start_detection_replay(
        source_id,
        operator_id=subject,
        reason_code="operator_replay_stale_attempt",
        operator_note="process ended",
        request_hash=request_hash,
        policy_id="detection_recovery_v1",
        policy_version="1",
        stale_after_seconds=900,
        max_attempts_per_comparison=3,
        detector_version="detector-auth-test",
        workflow_version=comparison_store.WORKFLOW_VERSION,
        previous_source_hash="prev-hash",
        current_source_hash="curr-hash",
        actor_auth_method="local_hs256",
        actor_token_id="jti_replay_first",
        actor_policy_id=POLICY.policy_id,
        actor_policy_version=POLICY.policy_version,
        now=NOW,
        db_path=db,
    )
    assert created is True
    assert row["comparison_id"] == comparison_id
    assert row["operator_id"] == subject
    assert row["actor_auth_method"] == "local_hs256"
    assert row["actor_token_id"] == "jti_replay_first"
    assert row["actor_policy_id"] == POLICY.policy_id
    assert row["actor_policy_version"] == POLICY.policy_version

    # A new token for the same subject does not change request identity.
    replayed, created_again = comparison_store.start_detection_replay(
        source_id,
        operator_id=subject,
        reason_code="operator_replay_stale_attempt",
        operator_note="process ended",
        request_hash=request_hash,
        policy_id="detection_recovery_v1",
        policy_version="1",
        stale_after_seconds=900,
        max_attempts_per_comparison=3,
        detector_version="detector-auth-test",
        workflow_version=comparison_store.WORKFLOW_VERSION,
        previous_source_hash="prev-hash",
        current_source_hash="curr-hash",
        actor_auth_method="local_hs256",
        actor_token_id="jti_replay_second",
        actor_policy_id=POLICY.policy_id,
        actor_policy_version=POLICY.policy_version,
        now=NOW,
        db_path=db,
    )
    assert created_again is False
    assert replayed["replay_id"] == row["replay_id"]
    assert replayed["actor_token_id"] == "jti_replay_first"
    assert "jti_replay_first" not in request_hash
    assert TEST_SECRET not in request_hash

    other_hash = detection_recovery.replay_request_hash(
        source_attempt_id=source_id,
        operator_id="other-operator@example.local",
        reason_code="operator_replay_stale_attempt",
        operator_note="process ended",
        policy_id="detection_recovery_v1",
        policy_version="1",
    )
    assert other_hash != request_hash
    with pytest.raises(comparison_store.DetectionStateError) as excinfo:
        comparison_store.start_detection_replay(
            source_id,
            operator_id="other-operator@example.local",
            reason_code="operator_replay_stale_attempt",
            operator_note="process ended",
            request_hash=other_hash,
            policy_id="detection_recovery_v1",
            policy_version="1",
            stale_after_seconds=900,
            max_attempts_per_comparison=3,
            detector_version="detector-auth-test",
            workflow_version=comparison_store.WORKFLOW_VERSION,
            previous_source_hash="prev-hash",
            current_source_hash="curr-hash",
            actor_auth_method="local_hs256",
            actor_token_id="jti_other",
            actor_policy_id=POLICY.policy_id,
            actor_policy_version=POLICY.policy_version,
            now=NOW,
            db_path=db,
        )
    assert excinfo.value.code == comparison_store.REASON_REPLAY_ALREADY_EXISTS


def _seed_review_item(db_path: Path) -> str:
    comparison_store.init_db(db_path)
    review_id = "crev_auth_storage"
    timestamp = NOW.isoformat()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            INSERT INTO comparisons (
                comparison_id, schema_version, workflow_version,
                previous_filing_id, current_filing_id, section_scope, status,
                created_at, updated_at, failure_code, failure_summary
            ) VALUES ('cmp_review_auth', ?, ?, 'previous', 'current', ?,
                      'detected', ?, ?, NULL, NULL)
            """,
            (
                comparison_store.COMPARISON_SCHEMA_VERSION,
                comparison_store.WORKFLOW_VERSION,
                '["item_1a_risk_factors"]',
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO comparison_governance_evaluations (
                evaluation_id, comparison_id, comparison_result_hash,
                policy_id, policy_version, risk_score, risk_level, decision,
                reason_codes, evaluated_at, governed_result_json,
                governed_result_hash
            ) VALUES ('gov_review_auth', 'cmp_review_auth', ?, 'risk', '1',
                      0.9, 'high', 'held_for_review', '[]', ?, '{}', ?)
            """,
            ("a" * 64, timestamp, "b" * 64),
        )
        conn.execute(
            """
            INSERT INTO comparison_review_items (
                review_id, comparison_id, evaluation_id,
                comparison_result_hash, governed_result_hash, status,
                terminal_event_id, decided_at, created_at
            ) VALUES (?, 'cmp_review_auth', 'gov_review_auth', ?, ?,
                      'pending', NULL, NULL, ?)
            """,
            (review_id, "a" * 64, "b" * 64, timestamp),
        )
    return review_id


def test_authenticated_review_event_persists_narrow_attribution_and_reopens(
    tmp_path,
):
    db = tmp_path / "review.db"
    review_id = _seed_review_item(db)
    row, created = comparison_store.decide_review(
        review_id,
        event_id="rev_evt_auth_storage",
        action="approved",
        reviewer_id="reviewer@example.local",
        reason_code="approved_as_is",
        reviewer_note="checked",
        request_hash="review-request-hash",
        original_governed_result_hash="b" * 64,
        final_reviewed_result_hash="c" * 64,
        reviewed_result_json="{}",
        edit_summary_json=None,
        actor_auth_method="local_hs256",
        actor_token_id="jti_review",
        actor_policy_id=POLICY.policy_id,
        actor_policy_version=POLICY.policy_version,
        db_path=db,
    )
    assert created is True
    assert row["reviewer_id"] == "reviewer@example.local"
    assert row["actor_auth_method"] == "local_hs256"
    assert row["actor_token_id"] == "jti_review"
    assert row["actor_policy_id"] == POLICY.policy_id
    assert row["actor_policy_version"] == POLICY.policy_version

    comparison_store.init_db(db)
    reopened = comparison_store.list_review_events(review_id, db_path=db)
    assert reopened[0]["actor_auth_method"] == "local_hs256"
    assert reopened[0]["actor_token_id"] == "jti_review"
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _create_legacy_actor_tables(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE comparison_detection_replays (
                replay_id TEXT PRIMARY KEY,
                comparison_id TEXT NOT NULL,
                source_attempt_id TEXT NOT NULL UNIQUE,
                replacement_attempt_id TEXT NOT NULL UNIQUE,
                operator_id TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                operator_note TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                policy_id TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                requested_at TEXT NOT NULL
            );
            INSERT INTO comparison_detection_replays VALUES (
                'rpl_legacy', 'cmp_legacy', 'att_old', 'att_new',
                'legacy-operator', 'operator_replay_stale_attempt', 'note',
                'hash', 'detection_recovery_v1', '1',
                '2026-01-01T00:00:00+00:00'
            );
            CREATE TABLE comparison_review_events (
                event_id TEXT PRIMARY KEY,
                review_id TEXT NOT NULL UNIQUE,
                comparison_id TEXT NOT NULL,
                evaluation_id TEXT NOT NULL,
                action TEXT NOT NULL,
                reviewer_id TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                reviewer_note TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                original_governed_result_hash TEXT NOT NULL,
                final_reviewed_result_hash TEXT NOT NULL,
                reviewed_result_json TEXT NOT NULL,
                edit_summary_json TEXT,
                created_at TEXT NOT NULL
            );
            INSERT INTO comparison_review_events VALUES (
                'rev_evt_legacy', 'crev_legacy', 'cmp_legacy', 'gov_legacy',
                'approved', 'legacy-reviewer', 'approved_as_is', 'note',
                'hash', 'old', 'new', '{}', NULL,
                '2026-01-01T00:00:00+00:00'
            );
            """
        )


def test_legacy_migration_is_idempotent_readable_and_never_invents_auth(tmp_path):
    db = tmp_path / "legacy.db"
    _create_legacy_actor_tables(db)
    comparison_store.init_db(db)
    comparison_store.init_db(db)

    replay = comparison_store.get_detection_replay_for_source(
        "att_old", db_path=db
    )
    review = comparison_store.list_review_events("crev_legacy", db_path=db)[0]
    for row in (replay, review):
        assert row["actor_auth_method"] == "legacy_self_asserted"
        assert row["actor_token_id"] is None
        assert row["actor_policy_id"] is None
        assert row["actor_policy_version"] is None

    with closing(sqlite3.connect(db)) as conn:
        for table in (
            "comparison_detection_replays",
            "comparison_review_events",
        ):
            columns = {
                row[1] for row in conn.execute(f"PRAGMA table_info({table})")
            }
            assert {
                "actor_auth_method",
                "actor_token_id",
                "actor_policy_id",
                "actor_policy_version",
            }.issubset(columns)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_concurrent_first_open_serializes_actor_migration(tmp_path):
    db = tmp_path / "concurrent-legacy.db"
    _create_legacy_actor_tables(db)
    gate = Barrier(2)

    def initialize():
        gate.wait()
        comparison_store.init_db(db)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(initialize) for _ in range(2)]
        for future in futures:
            future.result()

    with closing(sqlite3.connect(db)) as conn:
        for table in (
            "comparison_detection_replays",
            "comparison_review_events",
        ):
            columns = [
                row[1] for row in conn.execute(f"PRAGMA table_info({table})")
            ]
            assert columns.count("actor_auth_method") == 1
            assert columns.count("actor_token_id") == 1
            assert columns.count("actor_policy_id") == 1
            assert columns.count("actor_policy_version") == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_migrated_tables_enforce_the_same_actor_coherence_as_fresh_tables(
    tmp_path,
):
    migrated = tmp_path / "migrated.db"
    _create_legacy_actor_tables(migrated)
    comparison_store.init_db(migrated)

    fresh = tmp_path / "fresh.db"
    comparison_store.init_db(fresh)
    with closing(sqlite3.connect(fresh)) as conn, conn:
        # Foreign-key checks are intentionally off for these storage-constraint
        # probes; only the row-local actor invariant is under test.
        conn.execute(
            """
            INSERT INTO comparison_detection_replays (
                replay_id, comparison_id, source_attempt_id,
                replacement_attempt_id, operator_id, reason_code,
                operator_note, request_hash, policy_id, policy_version,
                requested_at
            ) VALUES (
                'rpl_fresh', 'cmp', 'att_old', 'att_new', 'legacy-operator',
                'operator_replay_stale_attempt', 'note', 'hash',
                'detection_recovery_v1', '1', ?
            )
            """,
            (NOW.isoformat(),),
        )
        conn.execute(
            """
            INSERT INTO comparison_review_events (
                event_id, review_id, comparison_id, evaluation_id, action,
                reviewer_id, reason_code, reviewer_note, request_hash,
                original_governed_result_hash, final_reviewed_result_hash,
                reviewed_result_json, edit_summary_json, created_at
            ) VALUES (
                'rev_evt_fresh', 'crev_fresh', 'cmp', 'gov', 'approved',
                'legacy-reviewer', 'approved_as_is', 'note', 'hash',
                'old', 'new', '{}', NULL, ?
            )
            """,
            (NOW.isoformat(),),
        )

    for db_path, replay_id, event_id in (
        (migrated, "rpl_legacy", "rev_evt_legacy"),
        (fresh, "rpl_fresh", "rev_evt_fresh"),
    ):
        with closing(sqlite3.connect(db_path)) as conn:
            with pytest.raises(sqlite3.IntegrityError, match="actor attribution"):
                with conn:
                    conn.execute(
                        "UPDATE comparison_detection_replays "
                        "SET actor_auth_method = 'local_hs256' "
                        "WHERE replay_id = ?",
                        (replay_id,),
                    )
            with pytest.raises(sqlite3.IntegrityError, match="actor attribution"):
                with conn:
                    conn.execute(
                        "UPDATE comparison_review_events "
                        "SET actor_auth_method = 'local_hs256' "
                        "WHERE event_id = ?",
                        (event_id,),
                    )


def test_direct_library_actor_defaults_remain_explicitly_legacy():
    attribution = comparison_store.validated_actor_attribution()
    assert attribution == {
        "actor_auth_method": "legacy_self_asserted",
        "actor_token_id": None,
        "actor_policy_id": None,
        "actor_policy_version": None,
    }


# --- Structured logging ------------------------------------------------------


def test_authenticated_logrecord_fields_are_allowlisted_and_token_safe(caplog):
    actor = {
        "actor_subject": "operator@example.local",
        "actor_auth_method": "local_hs256",
        "actor_token_id": "jti_log_record",
        "required_permission": "recovery.replay",
    }
    with caplog.at_level(logging.INFO, logger="comparison_reliability"):
        comparison_reliability.log_lifecycle_event(
            comparison_reliability.EVENT_REPLAY_CREATED,
            comparison_id="cmp_log",
            replay_id="rpl_log",
            source_attempt_id="att_old",
            actor_context=actor,
        )
    record = caplog.records[-1]
    for key, value in actor.items():
        assert getattr(record, key) == value
    assert record.comparison_id == "cmp_log"
    assert record.replay_id == "rpl_log"
    assert set(comparison_reliability.LOG_FIELDS).isdisjoint(
        {"authorization", "bearer_token", "secret", "claims", "operator_note"}
    )
    assert TEST_SECRET not in caplog.text


def test_review_decision_log_event_is_closed_and_logging_failure_is_nonfatal(
    monkeypatch,
):
    assert (
        comparison_reliability.EVENT_REVIEW_DECIDED
        == "comparison_review_decided"
    )

    def explode(*args, **kwargs):
        raise RuntimeError("logging unavailable")

    monkeypatch.setattr(comparison_reliability.logger, "info", explode)
    # Logging is after commit and deliberately cannot affect caller flow.
    comparison_reliability.log_lifecycle_event(
        comparison_reliability.EVENT_REVIEW_DECIDED,
        comparison_id="cmp",
        review_id="crev",
        review_event_id="rev_evt",
        review_action="approved",
        actor_context={
            "actor_subject": "reviewer",
            "actor_auth_method": "local_hs256",
            "actor_token_id": "jti",
            "required_permission": "review.decide",
        },
    )


def test_all_authenticated_mutation_log_events_keep_principal_context(caplog):
    actor = {
        "actor_subject": "actor@example.local",
        "actor_auth_method": "local_hs256",
        "actor_token_id": "jti_mutation_log",
    }
    cases = (
        (
            comparison_reliability.EVENT_COMPARISON_CREATED,
            {"comparison_id": "cmp_log", "status": "ready_for_detection"},
            "comparison.create",
        ),
        (
            comparison_reliability.EVENT_GOVERNANCE_EVALUATED,
            {
                "comparison_id": "cmp_log",
                "evaluation_id": "gov_log",
                "status": "held_for_review",
            },
            "governance.evaluate",
        ),
        (
            comparison_reliability.EVENT_EXPORT_CREATED,
            {
                "comparison_id": "cmp_log",
                "export_id": "exp_log",
                "status": "created",
            },
            "export.create",
        ),
    )
    assert set(comparison_reliability.AUTHENTICATED_MUTATION_LOG_EVENTS) == {
        event for event, _, _ in cases
    }

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="comparison_reliability"):
        for event, fields, permission in cases:
            comparison_reliability.log_lifecycle_event(
                event,
                actor_context={**actor, "required_permission": permission},
                **fields,
            )

    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None)
        in comparison_reliability.AUTHENTICATED_MUTATION_LOG_EVENTS
    ]
    assert [record.event for record in records] == [
        event for event, _, _ in cases
    ]
    assert all(record.actor_subject == actor["actor_subject"] for record in records)
    assert all(record.actor_token_id == actor["actor_token_id"] for record in records)
    assert [record.required_permission for record in records] == [
        permission for _, _, permission in cases
    ]


# --- Local token issuer CLI --------------------------------------------------


def _cli_env(secret: str | None = TEST_SECRET) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("AWS_") or key in {
            "TAVILY_API_KEY",
            "LANGSMITH_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
        }:
            env.pop(key, None)
    env[access_control.AUTH_SECRET_ENV] = "" if secret is None else secret
    return env


def _run_cli(*args: str, secret: str | None = TEST_SECRET, cwd=None):
    return subprocess.run(
        [sys.executable, str(CLI_PATH), *args],
        cwd=cwd or REPO_ROOT,
        env=_cli_env(secret),
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_plain_and_json_issue_valid_tokens_without_files(tmp_path):
    before = set(tmp_path.iterdir())
    plain = _run_cli(
        "--subject",
        "operator@example.local",
        "--role",
        "operator",
        "--ttl-seconds",
        "120",
        cwd=tmp_path,
    )
    assert plain.returncode == 0
    token = plain.stdout.strip()
    assert "\n" not in token
    principal = AUTHENTICATOR.verify(token)
    assert principal.subject == "operator@example.local"
    assert principal.roles == ("operator",)
    assert plain.stderr == ""

    structured = _run_cli(
        "--subject",
        "multi@example.local",
        "--role",
        "reviewer",
        "--role",
        "exporter",
        "--ttl-seconds",
        "120",
        "--json",
        cwd=tmp_path,
    )
    assert structured.returncode == 0
    payload = json.loads(structured.stdout)
    assert set(payload) == {
        "accessToken",
        "tokenType",
        "expiresAt",
        "subject",
        "roles",
    }
    assert payload["tokenType"] == "Bearer"
    assert payload["subject"] == "multi@example.local"
    assert payload["roles"] == ["exporter", "reviewer"]
    assert AUTHENTICATOR.verify(payload["accessToken"]).roles == (
        "exporter",
        "reviewer",
    )
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize(
    "args,secret",
    [
        (("--subject", "x", "--role", "unknown"), TEST_SECRET),
        (
            (
                "--subject",
                "x",
                "--role",
                "viewer",
                "--ttl-seconds",
                str(POLICY.max_token_ttl_seconds + 1),
            ),
            TEST_SECRET,
        ),
        (("--subject", "bad\nsubject", "--role", "viewer"), TEST_SECRET),
        (("--subject", "x", "--role", "viewer"), None),
        (("--subject", "x", "--role", "viewer"), "weak"),
    ],
    ids=(
        "unknown-role",
        "excessive-ttl",
        "invalid-subject",
        "missing-secret",
        "weak-secret",
    ),
)
def test_cli_refusals_exit_2_without_token_or_secret(args, secret):
    result = _run_cli(*args, secret=secret)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    if secret:
        assert secret not in result.stderr
    assert "eyJ" not in result.stderr


def test_cli_source_has_no_network_aws_or_token_file_mode():
    source = CLI_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "boto3",
        "requests",
        "httpx",
        "urllib",
        "socket",
        "AWS_ACCESS_KEY",
        "write_text",
        "open(",
        "--output",
        "--inspect",
    ):
        assert forbidden not in source


# --- Regression/security boundary guards ------------------------------------


def test_api_dtos_and_tracked_sources_contain_no_static_complete_token_or_secret():
    for model in (
        api.DetectionReplayDTO,
        api.ComparisonReviewEventDTO,
        api.ComparisonReviewDecisionDTO,
    ):
        fields = set(model.model_fields)
        assert not fields & {
            "accessToken",
            "authorization",
            "secret",
            "claims",
            "bearerToken",
        }

    source_files = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    bearer_jwt_prefix = "Authorization: Bearer " + "ey" + "J"
    compact_jwt = re.compile(
        r"(?<![A-Za-z0-9_-])"
        + "ey"
        + r"J[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{32,}"
        r"(?![A-Za-z0-9_-])"
    )
    for relative in source_files:
        path = REPO_ROOT / relative
        if path.suffix.lower() not in {
            ".py",
            ".md",
            ".yaml",
            ".yml",
            ".json",
            ".txt",
            ".example",
        }:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert bearer_jwt_prefix not in text, relative
        assert compact_jwt.search(text) is None, relative
        assert TEST_SECRET not in text, relative


def test_api_import_fails_closed_with_missing_secret_and_exposes_no_value():
    env = os.environ.copy()
    env[access_control.AUTH_SECRET_ENV] = ""
    result = subprocess.run(
        [sys.executable, "-c", "import api"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert TEST_SECRET not in combined
    assert "Authorization" not in combined
    assert "eyJ" not in combined


def test_policy_role_objects_are_immutable_and_deterministic():
    assert POLICY.roles == access_control.DEFINED_ROLES
    with pytest.raises(TypeError):
        POLICY.role_permissions["admin"] = ()  # type: ignore[index]
    for permissions in POLICY.role_permissions.values():
        assert isinstance(permissions, tuple)
        assert tuple(
            permission
            for permission in access_control.DEFINED_PERMISSIONS
            if permission in permissions
        ) == permissions
