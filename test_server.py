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
    monkeypatch.setattr(server, "ask", lambda question: f"answer to: {question}")
    response = client.post("/ask", json={"question": "how's DTC doing?"}, headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json() == {"answer": "answer to: how's DTC doing?"}


def test_ask_rejects_empty_question():
    response = client.post("/ask", json={"question": "   "}, headers=AUTH_HEADER)
    assert response.status_code == 400


def test_ask_returns_502_on_failure(monkeypatch):
    def _boom(question):
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
