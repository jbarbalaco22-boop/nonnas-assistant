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
