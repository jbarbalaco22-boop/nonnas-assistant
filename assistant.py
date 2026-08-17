"""The chat loop: takes a user question, lets Claude call read-only tools as needed, returns
an answer. This is the core logic — how it's exposed (web chat, CLI, Managed Agents) is a
separate, later decision. Runs standalone for now so it can be tested directly.
"""
import os

import anthropic
from dotenv import load_dotenv

from handlers import QboContext, ShopifyContext
from nonnas_shared.config import require_env
from nonnas_shared.connectors import qbo_client as shared_qbo
from nonnas_shared.connectors import shopify_client as shared_shopify
from tools import TOOL_SCHEMAS, dispatch

load_dotenv()

QBO_ENVIRONMENT = os.environ.get("QBO_ENVIRONMENT", "sandbox")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = """You are a financial Q&A assistant for Nonna's Italian Goods (Nonna's Olive \
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

Known data-quality context — apply this reasoning, don't just report raw numbers:
- TikTok and Amazon revenue post to QuickBooks via A2X as settlement-period summary entries, \
not raw order data. A gap between QuickBooks and Shopify for these channels may be settlement \
timing, not an error.
- Amazon's presence in Shopify is a known-incomplete, sometimes-duplicated mirror of real \
Amazon sales — there's no direct Amazon connection yet. Don't state Amazon unit/order counts \
from Shopify as complete.
- Wholesale revenue is often recorded as a bank Deposit with no Item or Quantity attached, so \
Wholesale COGS and units are frequently $0/zero even when real sales happened — that's a known \
gap, not evidence nothing sold.
- Historical COGS was corrected against a physical inventory count through July 2026. August \
2026 onward reflects the raw, uncorrected automated deduction — treat August's channel margins \
as less reliable than prior months' until a similar correction has been done.
- Ad spend accounts (Meta/Google/TikTok/Amazon Ads) aren't tagged by Class in QuickBooks — \
channel attribution for ads comes from mapping each platform to its channel, not from a \
QuickBooks Class field. Marketplace Advertising, Paid Collaborations, and Affiliate \
Commissions aren't confidently attributable to one channel and are excluded from per-channel \
figures rather than guessed at.

Be direct and quantitative. When you're not confident a number means what it looks like it \
means (e.g. a channel with $0 revenue that might just be a data gap), say so rather than \
stating it as fact."""


def _get_qbo_context() -> QboContext:
    client_id = require_env("QBO_CLIENT_ID")
    client_secret = require_env("QBO_CLIENT_SECRET")
    refresh_token = require_env("QBO_REFRESH_TOKEN")
    realm_id = require_env("QBO_REALM_ID")
    tokens = shared_qbo.refresh_access_token(client_id, client_secret, refresh_token)
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


def ask(question: str, client: anthropic.Anthropic | None = None) -> str:
    """Runs one question through the tool-use loop to completion, returns the final answer text."""
    client = client or anthropic.Anthropic()
    qbo_context = _get_qbo_context()
    shopify_context = _get_shopify_context()

    messages = [{"role": "user", "content": question}]

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
    import sys

    question = " ".join(sys.argv[1:]) or "What was TikTok's contribution margin last month?"
    print(f"Q: {question}\n")
    print(ask(question))
