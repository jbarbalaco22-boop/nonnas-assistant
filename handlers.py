"""Tool handler functions — the actual work behind each tool the assistant can call.
All read-only: these only ever call fetch_*/compute_* functions from nonnas_shared,
never anything that writes to QBO or Shopify.
"""
import glob
import json
import os
from datetime import date, datetime, timezone

from nonnas_shared.config import load_channel_units_by_month
from nonnas_shared.connectors import channel_financials
from nonnas_shared.connectors import qbo_client as shared_qbo
from nonnas_shared.connectors import shopify_client as shared_shopify
from nonnas_shared.connectors.shopify_channels import classify_source

CHANNELS = ["DTC", "TikTok", "Amazon", "Wholesale"]

DAILY_PACKET_GLOB = os.path.join(
    os.path.dirname(__file__), "..", "nonnas-daily-operator", "artifacts", "daily_packet_*.json"
)

UNITS_CAVEAT = (
    "Amazon and Wholesale unit/order counts here are known-incomplete. Amazon orders rarely "
    "flow through Shopify at all (a real Amazon connection doesn't exist yet). Wholesale "
    "revenue is typically recorded as a bank Deposit with no Item/Quantity, so it often shows "
    "zero units even when real sales happened. Treat DTC and TikTok numbers as reliable; treat "
    "Amazon and Wholesale as directional at best."
)


class QboContext:
    """Bundles what a live QBO call needs — access token already refreshed by the caller,
    not re-derived per tool call (refreshing on every tool call would burn through QBO's
    rotating refresh token far faster than necessary for one conversation)."""

    def __init__(self, access_token: str, realm_id: str, environment: str = "production"):
        self.access_token = access_token
        self.realm_id = realm_id
        self.environment = environment


class ShopifyContext:
    def __init__(self, domain: str, access_token: str):
        self.domain = domain
        self.access_token = access_token


def get_daily_snapshot() -> dict:
    """Reads the most recently generated Daily Packet — the default, fast path. Every response
    built from this MUST surface generated_at so the caller can tell the user how fresh this is.
    """
    matches = sorted(glob.glob(DAILY_PACKET_GLOB))
    if not matches:
        return {"error": "No Daily Packet has been generated yet — nonnas-daily-operator hasn't run."}
    with open(matches[-1], encoding="utf-8") as f:
        packet = json.load(f)
    return packet


def get_unit_reference() -> dict:
    """The canonical, hand-reconciled units-sold-by-channel reference (channel_units_by_month.csv
    in nonnas-shared), covering every month back to Sept 2024. Use this instead of
    get_channel_units_live for:
    - Any month before 2025-04, where there's no live per-channel data at all (Shopify wasn't
      split by channel yet - only a total-units figure exists, DTC/TikTok/Amazon/Wholesale are
      all null for those months).
    - A sanity check against live Amazon/Wholesale unit counts, which are known-unreliable (see
      the units caveat) - this reference was built by hand-reconciling against Amazon's and
      Faire's own transaction reports, not just Shopify's incomplete mirror of them.

    This is a fixed historical reference, not live - it doesn't extend past whatever month it
    was last updated through, and won't reflect anything more recent than that.
    """
    return {"months": load_channel_units_by_month()}


def get_channel_financials_live(qbo: QboContext, start_date: str, end_date: str, channel: str | None = None) -> dict:
    """Live pull: revenue/COGS/3PL/ads/fees/contribution margin per channel for an arbitrary
    date range. Use this for trend analysis (call it for several periods and compare) or when
    the cached Daily Packet isn't current enough for the question being asked.
    """
    account_map = channel_financials.load_qbo_account_map()
    pl_data = shared_qbo.fetch_profit_and_loss_by_class(
        qbo.realm_id, qbo.access_token,
        date.fromisoformat(start_date), date.fromisoformat(end_date),
        environment=qbo.environment,
    )
    channels_to_compute = [channel] if channel else CHANNELS
    results = {
        ch: channel_financials.compute_channel_margin(pl_data, ch, account_map)
        for ch in channels_to_compute
    }
    return {
        "start_date": start_date,
        "end_date": end_date,
        "pulled_at": datetime.now(timezone.utc).isoformat(),
        "channels": results,
        "income_statement": channel_financials.compute_net_income(pl_data),
    }


def get_channel_units_live(shopify: ShopifyContext, start_date: str, end_date: str) -> dict:
    """Live pull: orders/units/revenue per channel for an arbitrary date range, straight from
    Shopify. Always includes the Amazon/Wholesale reliability caveat — never state those two
    channels' figures as complete without it.
    """
    orders = shared_shopify.fetch_orders(
        shopify.domain, shopify.access_token,
        date.fromisoformat(start_date), date.fromisoformat(end_date),
    )
    buckets = {
        ch: {"orders": 0, "units": 0, "gross": 0.0, "discounts": 0.0, "refunds": 0.0, "net_revenue": 0.0}
        for ch in CHANNELS
    }
    for order in orders:
        channel = classify_source(order.get("sourceName"))
        if channel is None:
            continue
        breakdown = shared_shopify.order_revenue_breakdown(order)
        units = sum(node.get("quantity", 0) for node in order.get("lineItems", {}).get("nodes", []))
        buckets[channel]["orders"] += 1
        buckets[channel]["units"] += units
        buckets[channel]["gross"] += breakdown["gross"]
        buckets[channel]["discounts"] += breakdown["discounts"]
        buckets[channel]["refunds"] += breakdown["refunds"]
        buckets[channel]["net_revenue"] += breakdown["net"]

    return {
        "start_date": start_date,
        "end_date": end_date,
        "pulled_at": datetime.now(timezone.utc).isoformat(),
        "channels": buckets,
        "caveat": UNITS_CAVEAT,
    }


def get_dashboard_data(qbo: QboContext, shopify: ShopifyContext, start_date: str, end_date: str) -> dict:
    """Combines a live QBO pull and a live Shopify pull for the same period into one
    dashboard-ready structure: company-wide totals, per-channel margin + health metrics
    (AOV/discount rate/refund rate/ROAS) + revenue concentration.

    Computed live rather than from the cached Daily Packet on purpose — that packet only
    exists as a local file wherever nonnas-daily-operator last ran, which isn't reachable from
    wherever this backend is actually deployed (see nonnas-finance-audit/CLAUDE.md open items).
    A dashboard showing "no data" on first load would be a worse experience than one that's
    always live but costs a bit more per page view - acceptable at this usage scale (3 people,
    occasional checks, not constant polling).
    """
    financials = get_channel_financials_live(qbo, start_date, end_date)
    units = get_channel_units_live(shopify, start_date, end_date)

    channel_margins = financials["channels"]
    concentration = channel_financials.compute_revenue_concentration(channel_margins)

    channels = {}
    for ch in CHANNELS:
        margin = channel_margins[ch]
        shopify_totals = units["channels"][ch]
        health = channel_financials.compute_channel_health_metrics(margin, shopify_totals)
        channels[ch] = {
            **margin,
            **health,
            "revenue_share": concentration[ch],
            "orders": shopify_totals["orders"],
            "units": shopify_totals["units"],
        }

    company = channel_financials.compute_company_totals(channel_margins, units["channels"])
    net_income = financials["income_statement"]["net_income"]
    # overhead is derived from (contribution - net_income) rather than independently summed,
    # so it's always internally consistent with whatever the channel cards show - it silently
    # picks up unallocated ad spend and any G&A account not yet in qbo_account_map.json too.
    company["net_income"] = net_income
    company["overhead"] = company["contribution"] - net_income

    return {
        "start_date": start_date,
        "end_date": end_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "company": company,
        "channels": channels,
        "caveat": UNITS_CAVEAT,
    }
