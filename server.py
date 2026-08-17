"""Thin HTTP wrapper around assistant.ask() — this is what a web frontend actually talks to.
CORS is still wide open for now since there's no frontend origin to lock it down to yet — that
still needs tightening once a frontend exists. Auth is now in place (see auth.py).
"""
import sys

# Some minimal Linux container images (seen on Render's default Python environment) don't set
# a UTF-8 locale, which made model output containing an em-dash crash with a UnicodeEncodeError
# somewhere downstream of ask() - not reproducible on Windows even with PYTHONIOENCODING=ascii
# forced, so this is a defensive fix rather than one pinned to a confirmed single root cause.
# PYTHONUTF8=1 as a Render env var covers this too; this covers it even if that's forgotten on
# some future deployment target.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import logging
from datetime import date

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from assistant import _get_qbo_context, _get_shopify_context, ask
from auth import verify_token
from handlers import get_dashboard_data

logger = logging.getLogger("nonnas_assistant")
app = FastAPI(title="Harvest")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: lock to the actual frontend origin once one exists
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/whoami")
def whoami(user: str = Depends(verify_token)) -> dict:
    """Lets the frontend validate a token immediately on load (and show who's logged in)
    instead of waiting for the first real question to fail with a 401."""
    return {"user": user}


@app.get("/dashboard")
def dashboard_endpoint(
    start_date: str | None = None,
    end_date: str | None = None,
    user: str = Depends(verify_token),
) -> dict:
    """Defaults to month-to-date if no explicit range is given. Always computed live (see
    get_dashboard_data's docstring for why) - this is the endpoint the dashboard UI polls on
    load, not something meant to be hit at high frequency."""
    if not end_date:
        end_date = date.today().isoformat()
    if not start_date:
        start_date = date.today().replace(day=1).isoformat()

    qbo = _get_qbo_context()
    shopify = _get_shopify_context()
    try:
        return get_dashboard_data(qbo, shopify, start_date, end_date)
    except Exception as e:
        logger.exception("dashboard failed for %s to %s", start_date, end_date)
        raise HTTPException(status_code=502, detail=f"Failed to load dashboard: {e}") from e


@app.post("/ask", response_model=AskResponse)
def ask_endpoint(request: AskRequest, user: str = Depends(verify_token)) -> AskResponse:
    """Synchronous (not async def) on purpose — ask() is a blocking call (QBO/Shopify/Claude
    API round-trips), and FastAPI automatically runs plain `def` routes in a thread pool instead
    of blocking the event loop the way an `async def` route calling this would."""
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question must not be empty")
    try:
        answer = ask(question)
    except Exception as e:
        logger.exception("ask() failed for question: %r", question)
        raise HTTPException(status_code=502, detail=f"Failed to answer: {e}") from e
    return AskResponse(answer=answer)
