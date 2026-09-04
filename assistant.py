"""The chat loop: takes a user question, lets Claude call read-only tools as needed, returns
an answer. This is the core logic — how it's exposed (web chat, CLI, Managed Agents) is a
separate, later decision. Runs standalone for now so it can be tested directly.
"""
import os

import anthropic
from dotenv import load_dotenv

from handlers import QboContext, ShopifyContext
from nonnas_shared.config import load_business_context, require_env
from nonnas_shared.connectors import shopify_client as shared_shopify
from nonnas_shared.connectors.qbo_auth import refresh_access_token
from tools import TOOL_SCHEMAS, dispatch

load_dotenv()

QBO_ENVIRONMENT = os.environ.get("QBO_ENVIRONMENT", "sandbox")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = f"""You are a financial Q&A assistant for Nonna's Italian Goods (Nonna's Olive \
Oil), used by the CFO and the two founders to ask questions about the business — revenue, ad \
spend, COGS, gross margin, units sold, and trends, across the DTC (Shopify), TikTok Shop, \
Amazon, and Wholesale channels.

You are READ-ONLY. You have no tool that can write, post, or change anything in QuickBooks or \
Shopify — only report what's there. Never imply you took or could take an action beyond \
looking something up.

Data freshness: prefer get_daily_snapshot (fast, cached, refreshed once a day) for most \
questions, and ALWAYS state its generated_at timestamp in your answer so the reader knows how \
current the numbers are. Use the live tools (get_channel_financials_live, \
get_channel_units_live) when the question needs current-moment accuracy, spans a custom date \
range, or is about a trend across multiple periods — call the live financials tool once per \
period and compare.

Keep answers short and simple. Lead with the number or the direct answer. Skip long \
explanations, caveats, and tables unless the question actually calls for them or the person \
asks for more detail — this is a fast lookup tool, not a report generator. A couple of \
sentences beats a wall of text.

This chat interface renders plain text only — it does not parse Markdown. Never use **bold**, \
#headers, backticks, or -/* bullet or numbered-list syntax; those characters show up literally \
instead of formatting anything. For a single number or short answer, just write the sentence. \
When a question genuinely needs a multi-part breakdown (e.g. a number per channel), put each \
item on its own line as "Label: value" — a real line break already renders as a line break here, \
so that alone is enough structure without any markdown syntax.

If you don't have access to the data needed to answer — no tool covers it, or the underlying \
source doesn't track it — say so plainly ("I don't have data for that") rather than guessing, \
extrapolating, or making something up. The one exception: if explicitly asked to ESTIMATE or \
PROJECT something (e.g. "what will total sales be this year?"), it's fine to make a reasonable \
assumption from year-to-date data (e.g. "at the current pace") — just say clearly that it's an \
estimate and briefly state the assumption, never present it as a hard reported number.

{load_business_context()}

Be direct and quantitative. When you're not confident a number means what it looks like it \
means (e.g. a channel with $0 revenue that might just be a data gap), say so rather than \
stating it as fact."""


def _get_qbo_context() -> QboContext:
    import logging
    from pathlib import Path
    logger = logging.getLogger("nonnas_assistant")

    # Reload .env file if it exists (for local dev), but don't override environment variables
    # (so Render's dashboard-set variables take precedence)
    env_file = Path(__file__).resolve().parent / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)
    client_id = require_env("QBO_CLIENT_ID")
    client_secret = require_env("QBO_CLIENT_SECRET")
    refresh_token = require_env("QBO_REFRESH_TOKEN")
    realm_id = require_env("QBO_REALM_ID")

    # Validate that all required credentials are present and non-empty
    if not all([client_id, client_secret, refresh_token, realm_id]):
        logger.error(
            "Missing QBO credentials. Client ID: %s, Client Secret: %s, "
            "Refresh Token: %s, Realm ID: %s",
            "SET" if client_id else "MISSING",
            "SET" if client_secret else "MISSING",
            "SET" if refresh_token else "MISSING",
            "SET" if realm_id else "MISSING",
        )
        raise ValueError("One or more QBO credentials are missing from environment")

    logger.debug(
        "QBO auth attempt. Client ID: %s..., Realm ID: %s, "
        "Token length: %d, Environment: %s",
        client_id[:10],
        realm_id,
        len(refresh_token),
        QBO_ENVIRONMENT,
    )

    try:
        tokens = refresh_access_token(client_id, client_secret, refresh_token)
    except Exception as e:
        logger.error(
            "QBO token refresh failed: %s\nClient ID: %s..., Realm ID: %s, "
            "Refresh token length: %d, Environment: %s",
            str(e),
            client_id[:10],
            realm_id,
            len(refresh_token),
            QBO_ENVIRONMENT,
        )
        raise

    _persist_refresh_token(tokens["refresh_token"])
    return QboContext(tokens["access_token"], realm_id, QBO_ENVIRONMENT)


def _persist_refresh_token(new_refresh_token: str) -> None:
    """Same rationale as nonnas-daily-operator/src/main.py — QBO rotates the refresh token on
    every use, so the new one has to be saved or the next run/conversation inherits a dead
    token. See that module's docstring for the GitHub-Actions-shaped gap this doesn't solve."""
    from pathlib import Path

    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("QBO_REFRESH_TOKEN="):
            lines[i] = f"QBO_REFRESH_TOKEN={new_refresh_token}\n"
            env_path.write_text("".join(lines), encoding="utf-8")
            return


def _get_shopify_context() -> ShopifyContext:
    domain = require_env("SHOPIFY_STORE_DOMAIN")
    access_token = shared_shopify.get_access_token(domain)
    return ShopifyContext(domain, access_token)


MAX_TOOL_ITERATIONS = 6  # safety cap, independent of the Console-level monthly spend limit —
# a well-formed question shouldn't need more than a handful of tool calls to answer. If it
# does, something's more likely wrong (a confused model, a bad loop) than genuinely needing
# more data, so stop and say so rather than silently spending on retries.

# System prompt and tool schemas are identical on every call — including every round-trip
# within a single question's tool-use loop, and across separate questions asked close together.
# Marking the last block of each as a cache breakpoint means only the first call in a window
# pays full price; everything after reads the cache at 10% of the input token cost. Using the
# 1-hour cache (not the 5-minute default) since usage here is a handful of people asking
# occasional questions, not constant traffic — a 5-minute window would miss most of that reuse.
_CACHE_CONTROL = {"type": "ephemeral", "ttl": "1h"}
CACHED_SYSTEM = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": _CACHE_CONTROL}]
CACHED_TOOLS = [dict(t) for t in TOOL_SCHEMAS]
if CACHED_TOOLS:
    CACHED_TOOLS[-1] = {**CACHED_TOOLS[-1], "cache_control": _CACHE_CONTROL}


MAX_HISTORY_MESSAGES = 20  # ~10 prior exchanges — enough for follow-ups like "yes" or "what
# about last month" to resolve, without letting an old chat's context grow unbounded.


def ask(
    question: str,
    client: anthropic.Anthropic | None = None,
    history: list[dict] | None = None,
    selected_range: tuple[str, str] | None = None,
) -> str:
    """Runs one question through the tool-use loop to completion, returns the final answer text.

    `history` is prior turns as plain {"role": "user"|"assistant", "content": str} dicts (no
    tool_use/tool_result blocks - those are internal to a single question's loop and dropped
    once that question is answered). This is what lets a follow-up like "yes" or "what about
    Amazon" resolve against what was actually asked before, instead of every question starting
    from a blank slate.

    selected_range, if given, is the (start_date, end_date) the frontend's date-range picker is
    currently showing - passed through so a relative phrase like "this period" or "this month"
    resolves against whatever the user is actually looking at on screen, instead of the model
    guessing (previously it would fall back to e.g. "July vs. August MTD" with no connection to
    the selected range - real audit finding, 2026-08-18: asked with Year-to-Date selected, got
    an answer about July vs. August MTD instead). Prepended to the per-turn user message rather
    than folded into the cached system prompt, since this varies every request and the system
    prompt is prompt-cached for cost/latency - it's still just a hint, not a hard override: the
    model can and should use a different range if the question itself specifies one.
    """
    client = client or anthropic.Anthropic()
    qbo_context = _get_qbo_context()
    shopify_context = _get_shopify_context()

    prior = (history or [])[-MAX_HISTORY_MESSAGES:]
    if selected_range:
        range_note = (
            f"[For context: the dashboard the user is looking at is currently filtered to "
            f"{selected_range[0]} through {selected_range[1]}. If their question refers to "
            f"\"this period,\" \"this month,\" or similar without specifying a range, assume "
            f"they mean this one - unless the question itself clearly asks about a different "
            f"period, in which case use that instead.]\n\n{question}"
        )
    else:
        range_note = question
    messages = [*prior, {"role": "user", "content": range_note}]

    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=CACHED_SYSTEM,
            tools=CACHED_TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return "".join(block.text for block in response.content if block.type == "text")

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = dispatch(block.name, block.input, qbo_context, shopify_context)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": _to_json_text(result),
            })
        messages.append({"role": "user", "content": tool_results})
    else:
        return (
            f"I wasn't able to finish answering this within the usual number of steps "
            f"({MAX_TOOL_ITERATIONS} tool calls) — something may be off with the question or "
            f"the data rather than this genuinely needing more lookups. Try rephrasing or "
            f"narrowing the date range; let a human know if this keeps happening."
        )


def _to_json_text(result: dict) -> str:
    import json

    return json.dumps(result, default=str)


if __name__ == "__main__":
    import io
    import sys

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    question = " ".join(sys.argv[1:]) or "What was TikTok's contribution margin last month?"
    print(f"Q: {question}\n")
    print(ask(question))
