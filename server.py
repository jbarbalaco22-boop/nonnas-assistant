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
import traceback

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from assistant import ask
from auth import verify_token

logger = logging.getLogger("nonnas_assistant")
app = FastAPI(title="nonnas-assistant")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: lock to the actual frontend origin once one exists
    allow_methods=["POST"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/debug/env-check")
def debug_env_check(user: str = Depends(verify_token)) -> dict:
    """TEMPORARY - diagnosing why the ANTHROPIC_API_KEY header build fails only on Render, not
    locally, despite the same literal key value. Reports length and a safe repr, not the raw
    value, and only to an authenticated user. Remove once resolved."""
    import os

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    non_ascii_positions = [i for i, c in enumerate(key) if ord(c) > 127]
    return {
        "length": len(key),
        "repr": repr(key),
        "non_ascii_char_count": len(non_ascii_positions),
        "non_ascii_positions": non_ascii_positions[:20],
    }


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
        try:
            logger.exception("ask() failed for question: %r", question)
        except Exception:
            pass  # logging itself failed (plausibly the same encoding issue) - don't let that mask the real error
        # TEMPORARY debug aid: server-side logging of this specific error has been silently
        # failing (Python's logging module swallows errors that happen while emitting a
        # record), so put the traceback directly in the response instead, ASCII-safe-encoded so
        # it can't fail to transmit the same way. Remove once the root cause is found and fixed
        # - a stack trace in an API response is not something to ship long-term.
        safe_tb = traceback.format_exc().encode("ascii", errors="backslashreplace").decode("ascii")
        raise HTTPException(status_code=502, detail=f"Failed to answer: {e}\n\n{safe_tb}") from e
    return AskResponse(answer=answer)
