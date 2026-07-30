"""Offline API tests for the comparison routes (POST/GET/GET-list).

Follows the test_api_errors.py pattern: TestClient over the real app with
config paths monkeypatched to tmp storage. No Bedrock, no agent calls.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import api
import config
import filing_registry
from tests.auth_helpers import authorization_headers

client = TestClient(api.app, headers=authorization_headers())

PREV_ID = "acme-corporation:10-k:2024-12-31"
CURR_ID = "acme-corporation:10-k:2025-12-31"

_DTO_FIELDS = {
    "comparisonId",
    "schemaVersion",
    "workflowVersion",
    "previousFilingId",
    "currentFilingId",
    "sectionScope",
    "status",
    "createdAt",
    "updatedAt",
    "failureCode",
    "failureSummary",
}


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Tmp registry with the two Acme filings + tmp comparison database."""
    reg = tmp_path / "registry.jsonl"
    for source, filing_id, period_end, source_hash in (
        ("acme-2024.pdf", PREV_ID, "2024-12-31", "h24"),
        ("acme-2025.pdf", CURR_ID, "2025-12-31", "h25"),
    ):
        filing_registry.record_outcome(
            reg,
            source_path=source,
            source_name=source,
            source_hash=source_hash,
            parse_status=filing_registry.PARSED,
            filing_id=filing_id,
            company_key="acme corporation",
            company_name="Acme Corporation",
            form_type="10-k",
            period_end=period_end,
            document_family_id="acme-corp-10k-excerpt",
            identity_source="manifest",
        )
    db = tmp_path / "comparisons.db"
    monkeypatch.setattr(config, "FILING_REGISTRY_PATH", str(reg))
    monkeypatch.setattr(config, "COMPARISON_DB_PATH", str(db))
    return tmp_path


def _post(prev=PREV_ID, curr=CURR_ID, scope=None):
    body = {"previousFilingId": prev, "currentFilingId": curr}
    if scope is not None:
        body["sectionScope"] = scope
    return client.post("/api/comparisons", json=body)


def test_create_then_idempotent_return(storage):
    """201 + created=true first; 200 + created=false with the same id after."""
    first = _post()
    assert first.status_code == 201
    payload = first.json()
    assert payload["created"] is True
    comparison = payload["comparison"]
    assert set(comparison) == _DTO_FIELDS
    assert comparison["status"] == "ready_for_detection"
    assert comparison["sectionScope"] == ["item_1a_risk_factors"]

    second = _post()
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert (
        second.json()["comparison"]["comparisonId"] == comparison["comparisonId"]
    )


def test_invalid_pair_is_422_with_stable_reasons(storage):
    """Ineligible pairs are 422 with machine-readable codes, not 500s."""
    response = _post(prev=CURR_ID, curr=PREV_ID)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_comparison_pair"
    assert "period_order_invalid" in detail["reasons"]

    response = _post(prev="nope:10-k:2019-12-31")
    assert response.status_code == 422
    assert response.json()["detail"]["reasons"] == ["unknown_previous_filing"]

    response = _post(scope=["item_7_mdna"])
    assert response.status_code == 422
    assert response.json()["detail"]["reasons"] == ["unsupported_section_scope"]


def test_get_unknown_comparison_is_404(storage):
    """Test 17."""
    response = client.get("/api/comparisons/cmp_0000000000000000")
    assert response.status_code == 404
    assert response.json()["detail"] == "Comparison not found."


def test_get_returns_created_record(storage):
    created = _post().json()["comparison"]
    response = client.get(f"/api/comparisons/{created['comparisonId']}")
    assert response.status_code == 200
    assert response.json() == created


def test_list_filters_by_filing_id_and_status(storage):
    """Tests 18/19 at the API level."""
    created = _post().json()["comparison"]

    everything = client.get("/api/comparisons").json()
    assert [item["comparisonId"] for item in everything] == [
        created["comparisonId"]
    ]

    by_filing = client.get(
        "/api/comparisons", params={"filing_id": PREV_ID}
    ).json()
    assert len(by_filing) == 1
    none_for_other = client.get(
        "/api/comparisons", params={"filing_id": "globex-corporation:10-k:2025-12-31"}
    ).json()
    assert none_for_other == []

    ready = client.get(
        "/api/comparisons", params={"status": "ready_for_detection"}
    ).json()
    assert len(ready) == 1
    failed = client.get("/api/comparisons", params={"status": "failed"}).json()
    assert failed == []

    # Unknown status values are rejected by the Literal contract, not silently
    # matched to nothing.
    bad = client.get("/api/comparisons", params={"status": "completed"})
    assert bad.status_code == 422


def test_dto_exposes_no_paths_or_registry_internals(storage):
    """Test 20: no absolute paths, storage paths, or registry entry fields."""
    response = _post()
    text = response.text
    assert str(storage) not in text  # tmp root (registry + db live under it)
    assert "/Users/" not in text and "\\\\" not in text
    assert "source_path" not in text and "sourcePath" not in text
    assert "source_hash" not in text and "registry" not in text.lower()
    assert ".db" not in text and "sqlite" not in text.lower()

    listing = client.get("/api/comparisons")
    assert str(storage) not in listing.text
    for item in listing.json():
        assert set(item) == _DTO_FIELDS


def test_storage_failure_is_sanitized(storage, monkeypatch, caplog):
    """Test 21: SQLite errors never reach the client; correlation id logged."""
    secret = "unable to open database file /secret/comparisons/comparisons.db"

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError(secret)

    monkeypatch.setattr(api.comparison_store, "get_comparison", boom)
    import logging

    with caplog.at_level(logging.ERROR, logger="api"):
        response = client.get("/api/comparisons/cmp_whatever")

    assert response.status_code == 500
    assert "/secret" not in response.text
    assert "OperationalError" not in response.text
    assert "sqlite" not in response.text.lower()
    detail = response.json()["detail"]
    assert detail["code"] == "comparison_storage_error"
    assert detail["message"] == "Failed to access comparison storage."
    assert detail["error_id"].startswith("err_")
    # Full failure detail is preserved server-side with the correlation id.
    assert secret in caplog.text
    assert detail["error_id"] in caplog.text


def test_client_cannot_supply_identity_fields(storage):
    """POST accepts only the two filing ids and the scope; identity comes from
    the registry. Extra fields are rejected... or ignored per FastAPI default —
    assert the response's authoritative fields come from the registry, not the
    request."""
    response = client.post(
        "/api/comparisons",
        json={
            "previousFilingId": PREV_ID,
            "currentFilingId": CURR_ID,
            "companyKey": "evil corp",
            "formType": "8-k",
            "periodEnd": "1999-01-01",
        },
    )
    assert response.status_code in (200, 201)
    comparison = response.json()["comparison"]
    # No client-supplied identity appears anywhere in the entity.
    assert comparison["previousFilingId"] == PREV_ID
    assert comparison["currentFilingId"] == CURR_ID
    assert "companyKey" not in comparison and "formType" not in comparison
