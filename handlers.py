"""Tool handler functions — the actual work behind each tool the assistant can call.
All read-only: these only ever call fetch_*/compute_* functions from nonnas_shared,
never anything that writes to QBO or Shopify.
"""
import calendar
import glob
import json
import os
from datetime import date, datetime, timezone

from nonnas_shared.config import load_channel_units_by_month, load_sku_map
from nonnas_shared.connectors import channel_financials
from nonnas_shared.connectors import qbo_client as shared_qbo
from nonnas_shared.connectors import shopify_client as shared_shopify
from nonnas_shared.connectors.shopify_channels import classify_source
from nonnas_shared.connectors.sku_financials import compute_sku_revenue

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


def get_sku_units_live(shopify: ShopifyContext, start_date: str, end_date: str) -> dict:
    """Live pull: units sold per SKU, further broken out by channel, for an arbitrary date
    range - built ahead of the multi-SKU rollout (2026-08-18), for when "units by channel"
    alone stops being enough to know what's actually selling.

    Keyed by SKU code, each with its registered display name (via nonnas_shared.config's
    load_sku_map - None if not registered there, e.g. a brand-new SKU or a data-entry typo -
    still shows up under its raw Shopify SKU string rather than being silently dropped) and a
    per-channel unit breakdown: {"OO-OO-ORG-500": {"name": "...", "channels": {"DTC": 40, ...}}}.

    Units only, not revenue - Shopify's lineItems query (shopify_client.fetch_orders) currently
    fetches only sku/quantity per line, not price. Order-level discount/refund totals aren't
    allocated down to individual line items either (that needs an allocation method - e.g.
    proportional to line price - that hasn't been designed or verified yet). Use
    get_channel_units_live for the real, complete revenue figures; this is units-only until
    that's built out.
    """
    orders = shared_shopify.fetch_orders(
        shopify.domain, shopify.access_token,
        date.fromisoformat(start_date), date.fromisoformat(end_date),
    )
    sku_registry = load_sku_map()
    buckets: dict = {}
    for order in orders:
        channel = classify_source(order.get("sourceName"))
        if channel is None:
            continue
        for node in order.get("lineItems", {}).get("nodes", []):
            sku = node.get("sku") or "(no SKU)"
            qty = node.get("quantity", 0)
            entry = buckets.setdefault(sku, {
                "name": sku_registry.get(sku, {}).get("name"),  # None if not in the registry
                "channels": {ch: 0 for ch in CHANNELS},
            })
            entry["channels"][channel] = entry["channels"].get(channel, 0) + qty

    return {
        "start_date": start_date,
        "end_date": end_date,
        "pulled_at": datetime.now(timezone.utc).isoformat(),
        "skus": buckets,
    }


def get_sku_revenue_live(qbo: QboContext, start_date: str, end_date: str) -> dict:
    """Live pull: revenue and discounts per SKU for an arbitrary date range, recovered from raw
    JournalEntry transactions' free-text Description field - see nonnas_shared's
    fetch_journal_entries/compute_sku_revenue docstrings for why (QuickBooks' JournalEntry
    schema has no structured Product/Service field at all; A2X embeds the SKU as plain text
    instead, confirmed against a real test transaction on 2026-08-18).

    Deliberately not cached - re-pulls and re-parses every JournalEntry in the range on every
    call, so it's meaningfully slower than the other live tools (can take several seconds for a
    wide date range). This is the on-demand "Refresh SKU Data" button's backing call, not
    something to invoke on every dashboard load.

    Only covers whichever channels post through a connector that embeds SKU in the description
    text (confirmed for DTC via A2X as of 2026-08-18) - other channels' real activity in this
    range won't show up here at all, not because they had none. Also subject to QBO's query API
    lagging behind very recently created transactions (observed directly: a transaction
    retrievable by direct ID lookup didn't appear in ANY query for at least 20+ minutes after
    creation) - a $0/empty result for a period that should have activity may just mean "not
    indexed yet," not "nothing happened."

    Settlement-window caveat: this and get_sku_units_live are on two different clocks. Units
    come straight from Shopify (real-time, as orders are placed). Revenue here comes from A2X's
    settlement batches, which only post to QBO after a payout period closes - typically a few
    days behind. So for any range that includes recent days, units sold in Shopify will
    routinely be higher than what's shown here as settled revenue - that's a timing gap, not a
    data error, and it doesn't self-correct until the corresponding settlement posts. Don't
    diff these two tools' outputs for the same range and read the gap as missing/lost sales.
    """
    entries = shared_qbo.fetch_journal_entries(
        qbo.realm_id, qbo.access_token,
        date.fromisoformat(start_date), date.fromisoformat(end_date),
        environment=qbo.environment,
    )
    sku_registry = load_sku_map()
    revenue_by_sku = compute_sku_revenue(entries, sku_registry)
    return {
        "start_date": start_date,
        "end_date": end_date,
        "pulled_at": datetime.now(timezone.utc).isoformat(),
        "skus": revenue_by_sku,
        "journal_entries_scanned": len(entries),
    }


def _shift_back_one_month(d: date) -> date:
    """Same day-of-month, one calendar month earlier - clamped to the prior month's last day
    when it's shorter (e.g. Mar 31 -> Feb 28/29). Used to build the "same range last month"
    comparison period (Aug 1-17 -> Jul 1-17), not a trailing N-days-back window."""
    if d.month == 1:
        y, m = d.year - 1, 12
    else:
        y, m = d.year, d.month - 1
    last_day = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last_day))


def get_period_comparison(qbo: QboContext, start_date: str, end_date: str) -> dict:
    """Returns net_sales/contribution, company-wide and per channel, for the prior comparable
    period (same start/end day-of-month, one calendar month earlier) - powers the dashboard's
    period-over-period deltas.

    Deliberately QBO-only: skips the Shopify order fetch and health-metric computation
    get_dashboard_data does, since only net_sales/contribution are needed for a delta - doing
    the full computation for a period whose orders/units/ROAS are never displayed would roughly
    double this endpoint's external API calls for no reason.
    """
    prior_start = _shift_back_one_month(date.fromisoformat(start_date))
    prior_end = _shift_back_one_month(date.fromisoformat(end_date))
    financials = get_channel_financials_live(qbo, prior_start.isoformat(), prior_end.isoformat())
    channel_margins = financials["channels"]
    return {
        "start_date": prior_start.isoformat(),
        "end_date": prior_end.isoformat(),
        "company": {
            "net_sales": sum(m["net_sales"] for m in channel_margins.values()),
            "contribution": sum(m["contribution"] for m in channel_margins.values()),
        },
        "channels": {
            ch: {"net_sales": m["net_sales"], "contribution": m["contribution"]}
            for ch, m in channel_margins.items()
        },
    }


def get_dashboard_data(
    qbo: QboContext, shopify: ShopifyContext, start_date: str, end_date: str,
    include_prior_period: bool = True,
) -> dict:
    """Combines a live QBO pull and a live Shopify pull for the same period into one
    dashboard-ready structure: company-wide totals, per-channel margin + health metrics
    (AOV/discount rate/refund rate/ROAS) + revenue concentration.

    Computed live rather than from the cached Daily Packet on purpose — that packet only
    exists as a local file wherever nonnas-daily-operator last ran, which isn't reachable from
    wherever this backend is actually deployed (see nonnas-finance-audit/CLAUDE.md open items).
    A dashboard showing "no data" on first load would be a worse experience than one that's
    always live but costs a bit more per page view - acceptable at this usage scale (3 people,
    occasional checks, not constant polling).

    include_prior_period=False skips the extra QBO pull for period-over-period deltas -
    get_monthly_trend sets this, since /trends already gets month-over-month comparison for
    free from consecutive array entries, and doing it here too would roughly double the number
    of QBO calls a 6-month trend load makes.
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

    result = {
        "start_date": start_date,
        "end_date": end_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "company": company,
        "channels": channels,
        "caveat": UNITS_CAVEAT,
    }
    if include_prior_period:
        result["prior_period"] = get_period_comparison(qbo, start_date, end_date)
    return result


def get_monthly_trend(
    qbo: QboContext, shopify: ShopifyContext, months: int = 6, today: date | None = None
) -> list[dict]:
    """Returns get_dashboard_data()'s result for each of the last `months` calendar months,
    oldest first, including the current in-progress month (partial, ending today rather than
    month-end). Powers trend charts by reusing the exact same per-period computation the
    single-period dashboard uses, so a trend chart's numbers always match what the dashboard
    itself would show for that same period - no separate trend-specific math to keep in sync.

    `today` is injectable for deterministic testing; defaults to the real current date.
    """
    today = today or date.today()
    first_of_this_month = today.replace(day=1)

    period_starts = []
    y, m = first_of_this_month.year, first_of_this_month.month
    for _ in range(months):
        period_starts.append(date(y, m, 1))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    period_starts.reverse()

    results = []
    for i, period_start in enumerate(period_starts):
        is_current_month = i == len(period_starts) - 1
        if is_current_month:
            period_end = today
        else:
            last_day = calendar.monthrange(period_start.year, period_start.month)[1]
            period_end = period_start.replace(day=last_day)
        results.append(
            get_dashboard_data(
                qbo, shopify, period_start.isoformat(), period_end.isoformat(),
                include_prior_period=False,
            )
        )
    return results
