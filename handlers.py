"""Tool handler functions — the actual work behind each tool the assistant can call.
All read-only: these only ever call fetch_*/compute_* functions from nonnas_shared,
never anything that writes to QBO or Shopify.
"""
import calendar
import glob
import json
import os
from datetime import date, datetime, timedelta, timezone

from nonnas_shared.config import load_channel_units_by_month, load_qbo_account_map, load_sku_map
from nonnas_shared.connectors import channel_financials
from nonnas_shared.connectors import qbo_client as shared_qbo
from nonnas_shared.connectors import shopify_client as shared_shopify
from nonnas_shared.connectors.shopify_channels import classify_source
from nonnas_shared.connectors.sku_financials import compute_sku_revenue, sole_active_sku_as_of

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

    Keyed by SKU code, each with its registered display name and product grouping (via
    nonnas_shared.config's load_sku_map - None/the raw code if not registered there, e.g. a
    brand-new SKU or a data-entry typo, so it still shows up rather than being silently dropped),
    a per-channel breakdown of raw Shopify line quantity, and a pack-size-adjusted "units" total:
    {"OO-OO-ORG-501": {"name": "...", "product": "...", "pack_size": 3,
    "channels": {"DTC": 0, "Amazon": 5, ...}, "units": 15}}.

    "channels" is the raw Shopify line-item quantity (how many of that SKU/variant were ordered -
    e.g. 5 orders of a 3-pack shows as 5, not 15). "units" is that multiplied by the registry's
    pack_size (defaults to 1 for anything not registered, or not yet given a pack_size), which is
    the real bottle-equivalent count - 5 orders of a 3-pack is 15 actual bottles sold. Callers
    that want "how many actual bottles moved" (e.g. inventory, subtotal-by-product) should use
    units; callers that want "how many orders/line-items" should use channels' raw values.

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
            info = sku_registry.get(sku, {})
            entry = buckets.setdefault(sku, {
                "name": info.get("name"),  # None if not in the registry
                "product": info.get("product") or info.get("name") or sku,
                "pack_size": info.get("pack_size", 1),
                "channels": {ch: 0 for ch in CHANNELS},
            })
            entry["channels"][channel] = entry["channels"].get(channel, 0) + qty

    for entry in buckets.values():
        entry["units"] = sum(entry["channels"].values()) * entry["pack_size"]

    return {
        "start_date": start_date,
        "end_date": end_date,
        "pulled_at": datetime.now(timezone.utc).isoformat(),
        "skus": buckets,
    }


def get_sku_units_to_date(shopify: ShopifyContext, today: date | None = None) -> dict:
    """Total units sold per SKU since inception, combining two sources - the same "closed
    historical months are a trusted reference, the current month is always live" split used
    elsewhere in this codebase (e.g. get_monthly_trend):

    1. channel_units_by_month.csv's total_units, summed across every month strictly before the
       current one. This is necessary, not just a fallback: confirmed live (2026-08-18) that
       Shopify's own order API returns zero results for known-real historical months (Sept 2024,
       April 2025) that the CSV correctly has units for - Shopify's live API has some retention
       limit this store is well past for its early history, so there is no way to live-pull that
       far back. The CSV is the only source for it.
    2. get_sku_units_live for [first of the current month, today] - real, current-month Shopify
       data, correctly SKU-attributed via Shopify's own per-line SKU field (no guessing needed
       here, unlike the QBO revenue side - Shopify has always tracked this).

    Each historical month's total_units is attributed to whichever SKU was the registry's sole
    active one as of that month's own end date (nonnas_shared.connectors.sku_financials'
    sole_active_sku_as_of - the same per-transaction-date rule used on the revenue side, and for
    the same reason: OO-OO-ORG-501 going active mid-August 2026 must not retroactively make
    2024/2025 months ambiguous). A month where two-plus SKUs were already active by its end is
    excluded rather than guessed at - conservative, matching the "never guess" principle
    throughout this module. The live current-month portion is already properly SKU-bucketed via
    Shopify's own per-line SKU field, so it needs no fallback at all.
    """
    today = today or date.today()
    current_month = today.strftime("%Y-%m")
    reference = load_channel_units_by_month()
    sku_registry = load_sku_map()

    skus: dict = {}
    for month, m in reference.items():
        if month >= current_month or m["total_units"] is None:
            continue
        year, mo = (int(part) for part in month.split("-"))
        month_end = date(year, mo, calendar.monthrange(year, mo)[1]).isoformat()
        fallback_sku = sole_active_sku_as_of(sku_registry, month_end)
        if fallback_sku is None:
            continue
        # Every historical month predates any pack-size SKU's launch (see sole_active_sku_as_of),
        # so the CSV's total_units is already a real bottle count - no pack_size multiplier needed.
        info = sku_registry.get(fallback_sku, {})
        bucket = skus.setdefault(fallback_sku, {
            "name": info.get("name"),
            "product": info.get("product") or info.get("name") or fallback_sku,
            "units_to_date": 0,
        })
        bucket["units_to_date"] += m["total_units"]

    reference_through = max((m for m in reference if m < current_month), default=None)

    live = get_sku_units_live(shopify, f"{current_month}-01", today.isoformat())
    for sku, entry in live["skus"].items():
        bucket = skus.setdefault(sku, {
            "name": entry["name"],
            "product": entry["product"],
            "units_to_date": 0,
        })
        bucket["units_to_date"] += entry["units"]  # pack_size-adjusted, not raw line quantity

    return {
        "skus": skus,
        "reference_through": reference_through,
        "live_from": live["start_date"],
        "live_to": live["end_date"],
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


# Shopify's Orders API only returns orders from roughly the last 60 days for apps without the
# read_all_orders scope (Shopify's own documented limit). Confirmed empirically 2026-08-18: a
# single-day live pull for 61 days before today still returned real orders, 62 days before did
# not. Kept a few days under that observed edge as a safety margin, since the exact cutoff isn't
# something this codebase controls or can rely on being pinned to the day.
SHOPIFY_LIVE_LOOKBACK_DAYS = 55


def get_sku_units_for_period(
    shopify: ShopifyContext, start_date: str, end_date: str, today: date | None = None
) -> dict:
    """Units sold per SKU for an arbitrary [start_date, end_date], combining a live Shopify pull
    with the hand-reconciled channel_units_by_month.csv reference whenever the request reaches
    further back than Shopify's own order API can retrieve (see SHOPIFY_LIVE_LOOKBACK_DAYS).

    A request entirely within the live-retrievable window is answered purely from Shopify, same
    as before. A request that reaches further back pulls the historical portion from the
    reference instead - necessary, not optional: a pure live pull for an old date range doesn't
    error, it just silently returns near-empty real data, which would look like "nothing sold"
    rather than "can't see that far back."

    The reference is monthly, not daily, so only whole months fully inside [start_date, end_date]
    AND fully before the live boundary get included - a request that doesn't align to month
    boundaries can lose precision at the edges. Days not covered by either a whole reference
    month or the live pull are a real gap, not silently dropped - see the `gap_days` field.

    Each reference month is attributed via sole_active_sku_as_of at that month's own end date -
    see get_sku_units_to_date's docstring for why. Reference-month contributions use total_units
    directly (already a real bottle count - see get_sku_units_to_date); live contributions use
    the pack_size-adjusted "units" field from get_sku_units_live, not raw line quantity.

    When any reference months are used, per-channel/order-level detail is not available for
    those months (the CSV only has a company-wide monthly total) - so this returns unit totals
    only in that case, not a channel breakdown, to avoid presenting mismatched precision as if
    it were all equally granular.
    """
    today = today or date.today()
    live_boundary = today - timedelta(days=SHOPIFY_LIVE_LOOKBACK_DAYS)
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)

    reference = load_channel_units_by_month()
    sku_registry = load_sku_map()
    skus: dict = {}
    reference_months_used: list[str] = []
    covered: list[tuple[date, date]] = []

    for month, m in reference.items():
        if m["total_units"] is None:
            continue
        year, mo = (int(part) for part in month.split("-"))
        month_start = date(year, mo, 1)
        month_end = date(year, mo, calendar.monthrange(year, mo)[1])
        if month_start < start or month_end > end or month_end >= live_boundary:
            continue
        fallback_sku = sole_active_sku_as_of(sku_registry, month_end.isoformat())
        if fallback_sku is None:
            continue
        info = sku_registry.get(fallback_sku, {})
        bucket = skus.setdefault(fallback_sku, {
            "name": info.get("name"),
            "product": info.get("product") or info.get("name") or fallback_sku,
            "units": 0,
        })
        bucket["units"] += m["total_units"]
        reference_months_used.append(month)
        covered.append((month_start, month_end))

    live_start = max(start, live_boundary)
    live_result = None
    if live_start <= end:
        live_result = get_sku_units_live(shopify, live_start.isoformat(), end.isoformat())
        for sku, entry in live_result["skus"].items():
            bucket = skus.setdefault(sku, {
                "name": entry["name"],
                "product": entry["product"],
                "pack_size": entry["pack_size"],
                "units": 0,
            })
            bucket["units"] += entry["units"]
            if not reference_months_used:
                bucket["channels"] = dict(entry["channels"])  # only mix in detail if uniform
        covered.append((live_start, end))

    covered.sort()
    gap_days: list[dict] = []
    cursor = start
    for c_start, c_end in covered:
        if c_start > cursor:
            gap_days.append({"start": cursor.isoformat(), "end": (c_start - timedelta(days=1)).isoformat()})
        cursor = max(cursor, c_end + timedelta(days=1))
    if cursor <= end:
        gap_days.append({"start": cursor.isoformat(), "end": end.isoformat()})

    return {
        "start_date": start_date,
        "end_date": end_date,
        "skus": skus,
        "reference_months_used": sorted(reference_months_used),
        "live_from": live_result["start_date"] if live_result else None,
        "live_to": live_result["end_date"] if live_result else None,
        "gap_days": gap_days,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


def get_sku_revenue_live(qbo: QboContext, start_date: str, end_date: str) -> dict:
    """Live pull: revenue, discounts, and refunds per SKU for an arbitrary date range, recovered
    from raw JournalEntry transactions' free-text Description field - see nonnas_shared's
    fetch_journal_entries/compute_sku_revenue docstrings for why (QuickBooks' JournalEntry
    schema has no structured Product/Service field at all; A2X embeds the SKU as plain text
    instead, confirmed against a real test transaction on 2026-08-18).

    Reconciling against the channel-level "DTC Net Sales" figure: this pull's net is DTC Net
    Sales minus Shipping Revenue (deliberately excluded here - shipping isn't tied to a specific
    SKU), plus a small ($10-ish) residual from lines whose Description is genuinely ambiguous
    (see sku_financials' "known residual gap" note) that get conservatively excluded rather than
    guessed at. A real July 2026 pull reconciled to within $11.18 of that basis on a ~$4,900
    range - confirmed by direct QBO account inspection, not assumed.

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

    Pre-SKU-posting history: entries from before SKU-level revenue posting was turned on in the
    connector default to the registry's sole "active" SKU (OO-OO-ORG-500 as of 2026-08-18, the
    only SKU that was actually selling then) - see nonnas_shared.connectors.sku_financials'
    docstring for the exact rule. That default turns off automatically once a second SKU goes
    active.

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


def _shift_back_one_year(d: date) -> date:
    """Same month/day, one calendar year earlier - clamped for Feb 29 in a year that isn't
    leap (e.g. Feb 29, 2028 -> Feb 28, 2027)."""
    year = d.year - 1
    if d.month == 2 and d.day == 29 and calendar.monthrange(year, 2)[1] < 29:
        return date(year, 2, 28)
    return date(year, d.month, d.day)


def _prior_comparable_period(start: date, end: date) -> tuple[date, date]:
    """Picks the actually-meaningful prior period to compare against, based on the shape of the
    requested range, rather than always shifting back one calendar month - which is right for a
    single month-aligned range but wrong for anything else (a Year-to-Date range shifted back
    one month compares an ~8.5-month span to a ~7.5-month span against the wrong prior period
    entirely).

    - Month-aligned (starts on the 1st, ends within that same month) - Month-to-Date, or a
      fully-closed "Last Month" selection - compares to the same days one calendar month
      earlier. This is the one case the original single global rule got right.
    - Year-to-date (starts Jan 1 of the end date's year) - compares to the same days one
      calendar year earlier, since a partial year needs to be measured against the same partial
      year, not an arbitrary trailing month.
    - Anything else (Last 30 Days, a custom range) - compares to the immediately preceding
      period of equal length, ending the day before this range starts.
    """
    if start.day == 1 and end.year == start.year and end.month == start.month:
        return _shift_back_one_month(start), _shift_back_one_month(end)
    if start == date(end.year, 1, 1):
        return _shift_back_one_year(start), _shift_back_one_year(end)
    length = (end - start).days + 1
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=length - 1)
    return prior_start, prior_end


def get_period_comparison(qbo: QboContext, start_date: str, end_date: str) -> dict:
    """Returns net_sales/contribution/net_income, company-wide and per channel (net_income is
    company-wide only - it isn't a per-channel concept), for the prior comparable period - powers
    the dashboard's period-over-period deltas. See _prior_comparable_period's docstring for how
    the comparison period is chosen based on the requested range's shape.

    Deliberately QBO-only: skips the Shopify order fetch and health-metric computation
    get_dashboard_data does, since those aren't needed for a delta - doing the full computation
    for a period whose orders/units/ROAS are never displayed would roughly double this
    endpoint's external API calls for no reason.
    """
    prior_start, prior_end = _prior_comparable_period(
        date.fromisoformat(start_date), date.fromisoformat(end_date)
    )
    financials = get_channel_financials_live(qbo, prior_start.isoformat(), prior_end.isoformat())
    channel_margins = financials["channels"]
    return {
        "start_date": prior_start.isoformat(),
        "end_date": prior_end.isoformat(),
        "company": {
            "net_sales": sum(m["net_sales"] for m in channel_margins.values()),
            "contribution": sum(m["contribution"] for m in channel_margins.values()),
            "net_income": financials["income_statement"]["net_income"],
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


FOUNDER_PAYROLL_ACCOUNT_PREFIX = "71100 Founder/Officer Compensation"


def _estimate_next_payroll(qbo: QboContext, today: date) -> dict | None:
    """Looks back 120 days for Founder/Officer Compensation postings (flat, biweekly, via
    Paychex, per the business - confirmed live 2026-08-18: five real payments at a consistent
    14-day interval) and projects the next one from the last two payments' interval and amount.
    Returns None if fewer than two payments were found in the window - not enough to infer a
    cadence from.
    """
    lookback = today - timedelta(days=120)
    txns = shared_qbo.fetch_gl_account_transactions(
        qbo.realm_id, qbo.access_token, FOUNDER_PAYROLL_ACCOUNT_PREFIX, lookback, today,
        environment=qbo.environment,
    )
    if len(txns) < 2:
        return None
    txns.sort(key=lambda t: t["date"])
    last_date = date.fromisoformat(txns[-1]["date"])
    prior_date = date.fromisoformat(txns[-2]["date"])
    interval_days = (last_date - prior_date).days
    if interval_days <= 0:
        return None
    next_date = last_date + timedelta(days=interval_days)
    return {
        "amount": txns[-1]["amount"],
        "last_date": txns[-1]["date"],
        "cadence_days": interval_days,
        "next_estimated_date": next_date.isoformat(),
    }


def get_cash_snapshot(qbo: QboContext, today: date | None = None) -> dict:
    """Cash-basis snapshot for the Cash & Runway tab: combined bank balance, burn rate and
    runway computed from the ACTUAL change in that balance (not accrual Net Income - see below),
    plus a trailing 6-month balance trend and the one recurring obligation this business
    currently has a reliable automated signal for (founder payroll).

    Why cash-basis, not Net Income, for burn/runway: the whole reason this tab exists is that
    Net Income doesn't reflect cash reality - inventory purchases hit cash the day they're paid
    for, not when later recognized as COGS. Confirmed live (2026-08-18) this genuinely matters
    here: the trailing 90 days had accrual Net Income of -$81,448.59 but actual cash only fell
    $38,728.76 - a $42,719.83 gap. Net Income is still returned, as a supporting comparison
    figure, never as the burn number itself.

    Combined cash balance uses qbo_account_map.json's "bank_cash" list (the 2 operating accounts
    + the high-yield account, per the business) via fetch_gl_account_balance - GeneralLedger with
    an explicit end_date, not BalanceSheet (see that function's docstring for why). Cross-checked
    live against Account.CurrentBalance for the as-of-today case: exact match.

    Known obligations: this business doesn't currently track upcoming vendor bills/POs as
    forward-looking commitments (confirmed with the business 2026-08-18) - real Bill records do
    exist in QBO, but their due dates consistently land the same day as, or one day after, the
    transaction date, indicating they're entered when paid, not planned ahead of time. So the
    only obligation with a reliable automated signal here is founder payroll - everything else
    is a manual-entry list on the frontend, not pulled by this function.

    Several sequential QBO calls (balance x2, P&L, 6 monthly balance points, payroll history) -
    slower than the other live tools, same "on-demand, not polled" tradeoff already accepted for
    SKU Revenue.
    """
    today = today or date.today()
    lookback_start = today - timedelta(days=90)
    account_prefixes = load_qbo_account_map()["bank_cash"]

    balance_today = shared_qbo.fetch_gl_account_balance(
        qbo.realm_id, qbo.access_token, account_prefixes, today, environment=qbo.environment,
    )
    balance_90d_ago = shared_qbo.fetch_gl_account_balance(
        qbo.realm_id, qbo.access_token, account_prefixes, lookback_start, environment=qbo.environment,
    )
    net_cash_change_90d = balance_today - balance_90d_ago
    monthly_burn = -net_cash_change_90d / 3  # positive = burning cash, negative = growing
    runway_months = (balance_today / monthly_burn) if monthly_burn > 0 else None

    pl_data = shared_qbo.fetch_profit_and_loss_by_class(
        qbo.realm_id, qbo.access_token, lookback_start, today, environment=qbo.environment,
    )
    net_income_90d = channel_financials.compute_net_income(pl_data)["net_income"]

    # Trailing 6-month balance trend - same "6 months ending this month" window get_monthly_trend
    # uses elsewhere; one GL pull per month-end (current month uses today, not month-end, same as
    # get_monthly_trend's "always in progress" current period).
    first_of_this_month = today.replace(day=1)
    y, m = first_of_this_month.year, first_of_this_month.month
    month_starts = []
    for _ in range(6):
        month_starts.append(date(y, m, 1))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    month_starts.reverse()

    balance_trend = []
    for i, month_start in enumerate(month_starts):
        is_current = i == len(month_starts) - 1
        if is_current:
            as_of = today
        else:
            last_day = calendar.monthrange(month_start.year, month_start.month)[1]
            as_of = date(month_start.year, month_start.month, last_day)
        balance = shared_qbo.fetch_gl_account_balance(
            qbo.realm_id, qbo.access_token, account_prefixes, as_of, environment=qbo.environment,
        )
        balance_trend.append({"month": month_start.isoformat()[:7], "as_of": as_of.isoformat(), "balance": balance})

    return {
        "as_of": today.isoformat(),
        "cash_balance": balance_today,
        "cash_balance_90d_ago": balance_90d_ago,
        "lookback_start": lookback_start.isoformat(),
        "net_cash_change_90d": net_cash_change_90d,
        "monthly_burn": monthly_burn,
        "runway_months": runway_months,
        "net_income_90d": net_income_90d,
        "balance_trend": balance_trend,
        "known_payroll": _estimate_next_payroll(qbo, today),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
