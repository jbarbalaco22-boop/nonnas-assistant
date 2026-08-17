import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import auth  # noqa: E402


def test_valid_token_returns_matching_username(monkeypatch):
    monkeypatch.setenv("ASSISTANT_USERS", json.dumps({"cfo": "tok-cfo", "founder_1": "tok-f1"}))

    class _Creds:
        credentials = "tok-f1"

    assert auth.verify_token(_Creds()) == "founder_1"


def test_invalid_token_rejected(monkeypatch):
    monkeypatch.setenv("ASSISTANT_USERS", json.dumps({"cfo": "tok-cfo"}))

    class _Creds:
        credentials = "wrong-token"

    try:
        auth.verify_token(_Creds())
        assert False, "expected 401"
    except Exception as e:
        assert getattr(e, "status_code", None) == 401


def test_missing_credentials_rejected():
    try:
        auth.verify_token(None)
        assert False, "expected 401"
    except Exception as e:
        assert getattr(e, "status_code", None) == 401
