"""QuickBooks Online OAuth 2.0 — one-time (re)authorization for this app's independent QBO
connection. The actual token exchange/refresh calls live in the shared
nonnas_shared.connectors.qbo_auth module.

Uses the same hosted callback page nonnas-finance-audit's qb_auth.py uses (not a local server),
since Production redirect URIs on Intuit's platform don't accept plain http://localhost — the
page at REDIRECT_URI displays the Authorization Code, Realm ID, and State for manual copy/paste.

This app's Render deployment holds the real, currently-live QBO_CLIENT_ID/QBO_CLIENT_SECRET -
there is no local .env for this app by default. To run this:

    1. Copy QBO_CLIENT_ID and QBO_CLIENT_SECRET from Render (the nonnas-assistant backend
       service -> Environment) into a local .env in this directory (copy .env.example first
       if you don't have one yet). QBO_ENVIRONMENT should already say "production".
    2. In the Intuit Developer app for THIS connection (the one QBO_CLIENT_ID belongs to),
       confirm this redirect URI is registered under the Production tab:
           https://jbarbalaco22-boop.github.io/nonnas-legal/oauth-callback.html
       (If it's only registered for a different app's Client ID, add it here too - Intuit
       checks the redirect URI against the specific Client ID used in the request.)
    3. Run:  python qb_auth.py
    4. The script prints the new refresh_token and realm_id, and also writes them into your
       local .env for convenience. Copy the printed refresh_token into Render's
       QBO_REFRESH_TOKEN env var (and QBO_REALM_ID too, though that shouldn't have changed)
       and save - Render will redeploy automatically.
"""
import base64
import os
import re
import time
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

from nonnas_shared.connectors.qbo_auth import authorization_url, exchange_code_for_tokens

load_dotenv()

CLIENT_ID = os.environ.get("QBO_CLIENT_ID")
CLIENT_SECRET = os.environ.get("QBO_CLIENT_SECRET")
REDIRECT_URI = "https://jbarbalaco22-boop.github.io/nonnas-legal/oauth-callback.html"

ENV_PATH = Path(__file__).resolve().parent / ".env"


def _run_authorization_flow() -> dict:
    if not CLIENT_ID or not CLIENT_SECRET:
        raise SystemExit(
            "QBO_CLIENT_ID / QBO_CLIENT_SECRET not set. Copy them from Render's nonnas-assistant "
            "backend service (Environment tab) into a local .env first - see this file's docstring."
        )

    state = base64.urlsafe_b64encode(os.urandom(16)).decode()
    auth_url = authorization_url(CLIENT_ID, REDIRECT_URI, state)

    print("Opening browser for QuickBooks authorization...")
    print(f"If it doesn't open automatically, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    print("Sign in as whichever QuickBooks user should own this connection, and approve access.")
    print("After you approve, a page will show the Authorization Code, Realm ID, and State.")
    code = input("Paste the Authorization Code here: ").strip()
    realm_id = input("Paste the Realm ID here: ").strip()
    returned_state = input("Paste the State value here: ").strip()

    if not code:
        raise SystemExit("Authorization failed: no code entered.")
    if returned_state != state:
        raise SystemExit("Authorization failed: state mismatch (possible CSRF) - expected " + state)

    return {"code": code, "realm_id": realm_id}


def _update_env_file(refresh_token: str, realm_id: str) -> None:
    """Writes QBO_REFRESH_TOKEN/QBO_REALM_ID into local .env, for convenience only - the value
    that actually matters is what gets pasted into Render, printed separately below."""
    if not ENV_PATH.exists():
        print(f"(No local .env found at {ENV_PATH} - skipping local write, printed values below still apply.)")
        return
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    updated = {"QBO_REFRESH_TOKEN": False, "QBO_REALM_ID": False}
    for i, line in enumerate(lines):
        for key, value in (("QBO_REFRESH_TOKEN", refresh_token), ("QBO_REALM_ID", realm_id)):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={value}\n"
                updated[key] = True
    for key, value in (("QBO_REFRESH_TOKEN", refresh_token), ("QBO_REALM_ID", realm_id)):
        if not updated[key]:
            lines.append(f"{key}={value}\n")
    ENV_PATH.write_text("".join(lines), encoding="utf-8")
    print(f"Also updated {ENV_PATH} for local use.")


if __name__ == "__main__":
    result = _run_authorization_flow()
    tokens = exchange_code_for_tokens(CLIENT_ID, CLIENT_SECRET, result["code"], REDIRECT_URI)

    print("\n" + "=" * 70)
    print("SUCCESS. Paste these into Render -> nonnas-assistant backend -> Environment:")
    print("=" * 70)
    print(f"QBO_REFRESH_TOKEN={tokens['refresh_token']}")
    print(f"QBO_REALM_ID={result['realm_id']}")
    print("=" * 70)
    print("Save in Render - it will redeploy automatically and pick up the new token.\n")

    _update_env_file(tokens["refresh_token"], result["realm_id"])
