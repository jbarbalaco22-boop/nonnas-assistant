"""Claude tool-use schema definitions, and the dispatcher that maps a tool call to its handler."""
import handlers

TOOL_SCHEMAS = [
    {
        "name": "get_daily_snapshot",
        "description": (
            "Get the most recent Daily Packet — revenue, spend, COGS, margin, and units by "
            "channel (DTC/TikTok/Amazon/Wholesale), refreshed once a day. This is the default "
            "source for most questions: fast, no live API calls. ALWAYS state the packet's "
            "generated_at timestamp when answering from this, so the user knows how fresh the "
            "data is. If the question needs data more current than that timestamp, use the "
            "live tools instead."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_channel_financials_live",
        "description": (
            "Live pull of revenue/COGS/3PL/ads/fees/contribution margin by channel for a "
            "specific date range, computed fresh from QuickBooks right now (not cached). Use "
            "this for trend analysis (call it multiple times across different periods and "
            "compare) or when the user explicitly wants current-moment data rather than the "
            "daily snapshot. This is real-time-as-of-right-now, not an average of the past."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                "channel": {
                    "type": "string",
                    "enum": ["DTC", "TikTok", "Amazon", "Wholesale"],
                    "description": "Omit to get all 4 channels at once.",
                },
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_channel_units_live",
        "description": (
            "Live pull of orders/units/net revenue by channel for a specific date range, "
            "straight from Shopify. Amazon and Wholesale figures from this tool are known-"
            "incomplete (no real Amazon connection yet; Wholesale revenue is often recorded "
            "without any unit count at all) — always pass that caveat along verbatim when "
            "citing Amazon or Wholesale numbers from this tool, don't just state them as fact."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_unit_reference",
        "description": (
            "The canonical, hand-reconciled units-sold-by-channel reference, covering every "
            "month back to September 2024. Use this instead of get_channel_units_live for any "
            "month before 2025-04 (no live per-channel data exists that far back — Shopify "
            "wasn't split by channel yet, only a total-units figure), or as a more trustworthy "
            "cross-check when live Amazon/Wholesale unit counts look wrong. This is a fixed "
            "historical snapshot, not live — it won't reflect anything more recent than whenever "
            "it was last updated. Takes no arguments; returns every month it has."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


def dispatch(tool_name: str, tool_input: dict, qbo_context, shopify_context) -> dict:
    if tool_name == "get_daily_snapshot":
        return handlers.get_daily_snapshot()
    if tool_name == "get_channel_financials_live":
        return handlers.get_channel_financials_live(
            qbo_context, tool_input["start_date"], tool_input["end_date"], tool_input.get("channel")
        )
    if tool_name == "get_channel_units_live":
        return handlers.get_channel_units_live(
            shopify_context, tool_input["start_date"], tool_input["end_date"]
        )
    if tool_name == "get_unit_reference":
        return handlers.get_unit_reference()
    raise ValueError(f"Unknown tool: {tool_name}")
