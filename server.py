"""Thin HTTP wrapper around assistant.ask() — this is what a web frontend actually talks to.
No auth yet (separate, deliberate next step, not an oversight) and CORS is wide open for now
since there's no frontend origin to lock it down to yet — both need tightening before this is
reachable from outside your own machine.
"""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from assistant import ask

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


@app.post("/ask", response_model=AskResponse)
def ask_endpoint(request: AskRequest) -> AskResponse:
    """Synchronous (not async def) on purpose — ask() is a blocking call (QBO/Shopify/Claude
    API round-trips), and FastAPI automatically runs plain `def` routes in a thread pool instead
    of blocking the event loop the way an `async def` route calling this would."""
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question must not be empty")
    try:
        answer = ask(question)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to answer: {e}") from e
    return AskResponse(answer=answer)
