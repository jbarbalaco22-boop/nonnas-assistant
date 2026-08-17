"""Bearer-token auth for exactly 3 known users (CFO + 2 founders). A full OAuth/session system
is overkill for a fixed, tiny, known set of people — a per-person static token is simpler,
auditable (each person's requests are attributable), and easy to revoke individually (just
remove their entry and redeploy) without affecting the other two.
"""
import json
import os

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_security = HTTPBearer(auto_error=False)


def load_users() -> dict[str, str]:
    """ASSISTANT_USERS is a JSON object: {"name": "token", ...}. Kept in .env, never committed,
    same as every other credential in this app."""
    raw = os.environ.get("ASSISTANT_USERS", "{}")
    return json.loads(raw)


def verify_token(credentials: HTTPAuthorizationCredentials | None = Depends(_security)) -> str:
    """FastAPI dependency — returns the matched username on success, raises 401 otherwise.
    Wire this into any endpoint that shouldn't be open to the public internet."""
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    users = load_users()
    for name, token in users.items():
        if credentials.credentials == token:
            return name

    raise HTTPException(status_code=401, detail="Invalid token")
