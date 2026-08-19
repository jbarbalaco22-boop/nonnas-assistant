"""Thin HTTP wrapper around assistant.ask() — this is what a web frontend actually talks to.
CORS is locked to the deployed frontend's origin (see ALLOWED_ORIGINS below). Auth is in place
(see auth.py).
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
from handlers import (
    get_cash_snapshot,
    get_dashboard_data,
    get_monthly_trend,
    get_sku_revenue_live,
    get_sku_units_for_period,
    get_sku_units_to_date,
)

logger = logging.getLogger("nonnas_assistant")
app = FastAPI(title="Harvest")

# The deployed Render Static Site, plus localhost for local dev testing of the frontend against
# this same deployed backend (the pattern used throughout this build).
ALLOWED_ORIGINS = [
    "https://nonnas-assistant-frontend.onrender.com",
    "http://localhost:8080",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class ChatMessage(BaseModel):
    role: str
    content: str


class AskRequest(BaseModel):
    question: str
    history: list[ChatMessage] = []
    selected_range_start: str | None = None
    selected_range_end: str | None = None


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


@app.get("/trends")
def trends_endpoint(
    months: int = 6,
    user: str = Depends(verify_token),
) -> dict:
    """Monthly trend data for the last `months` calendar months (including the current,
    in-progress one), reusing get_dashboard_data per month so trend numbers always match what
    the single-period dashboard shows for that same range. Several sequential QBO/Shopify
    pulls, so slower than /dashboard - the frontend loads this after the main dashboard, not
    blocking it."""
    qbo = _get_qbo_context()
    shopify = _get_shopify_context()
    try:
        return {"periods": get_monthly_trend(qbo, shopify, months)}
    except Exception as e:
        logger.exception("trends failed for last %s months", months)
        raise HTTPException(status_code=502, detail=f"Failed to load trends: {e}") from e


@app.get("/sku-revenue")
def sku_revenue_endpoint(
    start_date: str,
    end_date: str,
    user: str = Depends(verify_token),
) -> dict:
    """On-demand, no caching (see get_sku_revenue_live's docstring) - this is the "Refresh SKU
    Data" button's backing call, not something polled automatically. start_date/end_date are
    required (unlike /dashboard) since this is always an explicit, deliberate action, not a
    page-load default."""
    qbo = _get_qbo_context()
    try:
        return get_sku_revenue_live(qbo, start_date, end_date)
    except Exception as e:
        logger.exception("sku-revenue failed for %s to %s", start_date, end_date)
        raise HTTPException(status_code=502, detail=f"Failed to load SKU revenue: {e}") from e


@app.get("/sku-units")
def sku_units_endpoint(
    start_date: str,
    end_date: str,
    user: str = Depends(verify_token),
) -> dict:
    """On-demand pull for an arbitrary period - the "SKU Units" card's period-specific button.
    Combines live Shopify data with the historical reference as needed - see
    get_sku_units_for_period's docstring for why a pure live pull isn't enough for older ranges."""
    shopify = _get_shopify_context()
    try:
        return get_sku_units_for_period(shopify, start_date, end_date)
    except Exception as e:
        logger.exception("sku-units failed for %s to %s", start_date, end_date)
        raise HTTPException(status_code=502, detail=f"Failed to load SKU units: {e}") from e


@app.get("/sku-units-to-date")
def sku_units_to_date_endpoint(user: str = Depends(verify_token)) -> dict:
    """Total units sold per SKU since inception - see get_sku_units_to_date's docstring for how
    this combines the hand-reconciled historical reference with a live current-month pull.
    No date params: this always means "everything up to today," not a selectable range."""
    shopify = _get_shopify_context()
    try:
        return get_sku_units_to_date(shopify)
    except Exception as e:
        logger.exception("sku-units-to-date failed")
        raise HTTPException(status_code=502, detail=f"Failed to load units to date: {e}") from e


@app.get("/cash-snapshot")
def cash_snapshot_endpoint(user: str = Depends(verify_token)) -> dict:
    """Cash & Runway tab data - combined bank balance, cash-basis OPERATING burn rate/runway
    (excludes financing activity like equity/SAFE investments - see get_cash_snapshot's
    docstring for why), a trailing 6-month balance trend, and a full recurring-fixed-cost
    (Overhead) breakdown by account. Obligations beyond Overhead aren't pulled here (no reliable
    automated source - the frontend keeps a manual-entry list for that instead). No date params -
    always "as of today," like /sku-units-to-date."""
    qbo = _get_qbo_context()
    try:
        return get_cash_snapshot(qbo)
    except Exception as e:
        logger.exception("cash-snapshot failed")
        raise HTTPException(status_code=502, detail=f"Failed to load cash snapshot: {e}") from e


@app.post("/ask", response_model=AskResponse)
def ask_endpoint(request: AskRequest, user: str = Depends(verify_token)) -> AskResponse:
    """Synchronous (not async def) on purpose — ask() is a blocking call (QBO/Shopify/Claude
    API round-trips), and FastAPI automatically runs plain `def` routes in a thread pool instead
    of blocking the event loop the way an `async def` route calling this would."""
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question must not be empty")
    history = [{"role": m.role, "content": m.content} for m in request.history]
    selected_range = (
        (request.selected_range_start, request.selected_range_end)
        if request.selected_range_start and request.selected_range_end
        else None
    )
    try:
        answer = ask(question, history=history, selected_range=selected_range)
    except Exception as e:
        logger.exception("ask() failed for question: %r", question)
        raise HTTPException(status_code=502, detail=f"Failed to answer: {e}") from e
    return AskResponse(answer=answer)
