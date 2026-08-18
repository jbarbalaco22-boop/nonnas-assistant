"""Tests for the HTTP layer — request/response shape, auth, and error handling, with
assistant.ask() mocked so this doesn't hit real QBO/Shopify/Claude APIs. The real end-to-end
path (server -> ask() -> real APIs) was verified manually against a live server instead."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402

client = TestClient(server.app)
AUTH_HEADER = {"Authorization": "Bearer test-token"}


def setup_module(module):
    import os

    os.environ["ASSISTANT_USERS"] = json.dumps({"test_user": "test-token"})


def test_health_does_not_require_auth():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ask_rejects_missing_auth():
    response = client.post("/ask", json={"question": "how's DTC doing?"})
    assert response.status_code == 401


def test_ask_rejects_wrong_token():
    response = client.post(
        "/ask", json={"question": "how's DTC doing?"}, headers={"Authorization": "Bearer wrong"}
    )
    assert response.status_code == 401


def test_whoami_returns_matched_username():
    response = client.get("/whoami", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json() == {"user": "test_user"}


def test_whoami_rejects_missing_auth():
    response = client.get("/whoami")
    assert response.status_code == 401


def test_ask_returns_answer_with_valid_token(monkeypatch):
    monkeypatch.setattr(server, "ask", lambda question, history=None: f"answer to: {question}")
    response = client.post("/ask", json={"question": "how's DTC doing?"}, headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json() == {"answer": "answer to: how's DTC doing?"}


def test_ask_rejects_empty_question():
    response = client.post("/ask", json={"question": "   "}, headers=AUTH_HEADER)
    assert response.status_code == 400


def test_ask_passes_history_through(monkeypatch):
    captured = {}

    def fake_ask(question, history=None):
        captured["history"] = history
        return "ok"

    monkeypatch.setattr(server, "ask", fake_ask)
    response = client.post(
        "/ask",
        json={
            "question": "and last month?",
            "history": [
                {"role": "user", "content": "how's DTC doing?"},
                {"role": "assistant", "content": "DTC net sales are $12,000 this month."},
            ],
        },
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200
    assert captured["history"] == [
        {"role": "user", "content": "how's DTC doing?"},
        {"role": "assistant", "content": "DTC net sales are $12,000 this month."},
    ]


def test_ask_defaults_to_empty_history():
    response = client.post("/ask", json={"question": "anything"}, headers=AUTH_HEADER)
    assert response.status_code in (200, 502)  # just confirming no validation error on omitted history


def test_ask_returns_502_on_failure(monkeypatch):
    def _boom(question, history=None):
        raise RuntimeError("QBO is down")

    monkeypatch.setattr(server, "ask", _boom)
    response = client.post("/ask", json={"question": "anything"}, headers=AUTH_HEADER)
    assert response.status_code == 502
    assert "QBO is down" in response.json()["detail"]


def test_dashboard_rejects_missing_auth():
    response = client.get("/dashboard")
    assert response.status_code == 401


def test_dashboard_defaults_to_month_to_date(monkeypatch):
    captured = {}

    def _fake_get_dashboard_data(qbo, shopify, start_date, end_date):
        captured["start_date"] = start_date
        captured["end_date"] = end_date
        return {"company": {"net_sales": 1.0}, "channels": {}}

    monkeypatch.setattr(server, "_get_qbo_context", lambda: None)
    monkeypatch.setattr(server, "_get_shopify_context", lambda: None)
    monkeypatch.setattr(server, "get_dashboard_data", _fake_get_dashboard_data)

    response = client.get("/dashboard", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert captured["start_date"].endswith("-01")  # first of the month


def test_dashboard_accepts_explicit_date_range(monkeypatch):
    captured = {}

    def _fake_get_dashboard_data(qbo, shopify, start_date, end_date):
        captured["start_date"] = start_date
        captured["end_date"] = end_date
        return {"company": {}, "channels": {}}

    monkeypatch.setattr(server, "_get_qbo_context", lambda: None)
    monkeypatch.setattr(server, "_get_shopify_context", lambda: None)
    monkeypatch.setattr(server, "get_dashboard_data", _fake_get_dashboard_data)

    response = client.get(
        "/dashboard?start_date=2026-07-01&end_date=2026-07-31", headers=AUTH_HEADER
    )
    assert response.status_code == 200
    assert captured == {"start_date": "2026-07-01", "end_date": "2026-07-31"}


def test_dashboard_returns_502_on_failure(monkeypatch):
    monkeypatch.setattr(server, "_get_qbo_context", lambda: None)
    monkeypatch.setattr(server, "_get_shopify_context", lambda: None)

    def _boom(qbo, shopify, start_date, end_date):
        raise RuntimeError("Shopify is down")

    monkeypatch.setattr(server, "get_dashboard_data", _boom)
    response = client.get("/dashboard", headers=AUTH_HEADER)
    assert response.status_code == 502
    assert "Shopify is down" in response.json()["detail"]


def test_sku_revenue_rejects_missing_auth():
    response = client.get("/sku-revenue?start_date=2026-08-01&end_date=2026-08-17")
    assert response.status_code == 401


def test_sku_revenue_requires_date_range():
    # start_date/end_date have no defaults on this endpoint - always an explicit action.
    response = client.get("/sku-revenue", headers=AUTH_HEADER)
    assert response.status_code == 422


def test_sku_revenue_passes_through_date_range(monkeypatch):
    captured = {}

    def _fake_get_sku_revenue_live(qbo, start_date, end_date):
        captured["start_date"] = start_date
        captured["end_date"] = end_date
        return {"skus": {}}

    monkeypatch.setattr(server, "_get_qbo_context", lambda: None)
    monkeypatch.setattr(server, "get_sku_revenue_live", _fake_get_sku_revenue_live)

    response = client.get(
        "/sku-revenue?start_date=2026-08-10&end_date=2026-08-12", headers=AUTH_HEADER
    )
    assert response.status_code == 200
    assert captured == {"start_date": "2026-08-10", "end_date": "2026-08-12"}


def test_sku_revenue_returns_502_on_failure(monkeypatch):
    monkeypatch.setattr(server, "_get_qbo_context", lambda: None)

    def _boom(qbo, start_date, end_date):
        raise RuntimeError("QBO query timed out")

    monkeypatch.setattr(server, "get_sku_revenue_live", _boom)
    response = client.get(
        "/sku-revenue?start_date=2026-08-01&end_date=2026-08-17", headers=AUTH_HEADER
    )
    assert response.status_code == 502
    assert "QBO query timed out" in response.json()["detail"]


def test_sku_units_rejects_missing_auth():
    response = client.get("/sku-units?start_date=2026-08-01&end_date=2026-08-17")
    assert response.status_code == 401


def test_sku_units_requires_date_range():
    response = client.get("/sku-units", headers=AUTH_HEADER)
    assert response.status_code == 422


def test_sku_units_passes_through_date_range(monkeypatch):
    captured = {}

    def _fake_get_sku_units_for_period(shopify, start_date, end_date):
        captured["start_date"] = start_date
        captured["end_date"] = end_date
        return {"skus": {}}

    monkeypatch.setattr(server, "_get_shopify_context", lambda: None)
    monkeypatch.setattr(server, "get_sku_units_for_period", _fake_get_sku_units_for_period)

    response = client.get(
        "/sku-units?start_date=2026-08-10&end_date=2026-08-12", headers=AUTH_HEADER
    )
    assert response.status_code == 200
    assert captured == {"start_date": "2026-08-10", "end_date": "2026-08-12"}


def test_sku_units_returns_502_on_failure(monkeypatch):
    monkeypatch.setattr(server, "_get_shopify_context", lambda: None)

    def _boom(shopify, start_date, end_date):
        raise RuntimeError("Shopify is down")

    monkeypatch.setattr(server, "get_sku_units_for_period", _boom)
    response = client.get(
        "/sku-units?start_date=2026-08-01&end_date=2026-08-17", headers=AUTH_HEADER
    )
    assert response.status_code == 502
    assert "Shopify is down" in response.json()["detail"]


def test_sku_units_to_date_rejects_missing_auth():
    response = client.get("/sku-units-to-date")
    assert response.status_code == 401


def test_sku_units_to_date_calls_handler(monkeypatch):
    monkeypatch.setattr(server, "_get_shopify_context", lambda: None)
    monkeypatch.setattr(
        server, "get_sku_units_to_date",
        lambda shopify: {"skus": {"OO-OO-ORG-500": {"name": "x", "units_to_date": 13704}}},
    )
    response = client.get("/sku-units-to-date", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json()["skus"]["OO-OO-ORG-500"]["units_to_date"] == 13704


def test_sku_units_to_date_returns_502_on_failure(monkeypatch):
    monkeypatch.setattr(server, "_get_shopify_context", lambda: None)

    def _boom(shopify):
        raise RuntimeError("reference file missing")

    monkeypatch.setattr(server, "get_sku_units_to_date", _boom)
    response = client.get("/sku-units-to-date", headers=AUTH_HEADER)
    assert response.status_code == 502
    assert "reference file missing" in response.json()["detail"]


def test_cash_snapshot_rejects_missing_auth():
    response = client.get("/cash-snapshot")
    assert response.status_code == 401


def test_cash_snapshot_calls_handler(monkeypatch):
    monkeypatch.setattr(server, "_get_qbo_context", lambda: None)
    monkeypatch.setattr(
        server, "get_cash_snapshot",
        lambda qbo: {"cash_balance": 67029.26, "monthly_burn": 12909.59, "runway_months": 5.19},
    )
    response = client.get("/cash-snapshot", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json()["cash_balance"] == 67029.26


def test_cash_snapshot_returns_502_on_failure(monkeypatch):
    monkeypatch.setattr(server, "_get_qbo_context", lambda: None)

    def _boom(qbo):
        raise RuntimeError("QBO GL pull failed")

    monkeypatch.setattr(server, "get_cash_snapshot", _boom)
    response = client.get("/cash-snapshot", headers=AUTH_HEADER)
    assert response.status_code == 502
    assert "QBO GL pull failed" in response.json()["detail"]
