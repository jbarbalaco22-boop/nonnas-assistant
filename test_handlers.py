"""Tests for get_dashboard_data's combination logic — with get_channel_financials_live and
get_channel_units_live mocked, so this doesn't hit real QBO/Shopify APIs. The combination itself
(company totals, per-channel health metrics, revenue concentration) was also verified directly
against real July 2026 data, separately from these unit tests."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import handlers  # noqa: E402


def _financials_result():
    return {
        "channels": {
            "DTC": {"net_sales": 1000.0, "cogs": 300.0, "three_pl": 0.0, "ads": 100.0, "fees": 20.0,
                     "contribution": 580.0, "contribution_pct": 0.58},
            "TikTok": {"net_sales": 500.0, "cogs": 150.0, "three_pl": 0.0, "ads": 10.0, "fees": 10.0,
                       "contribution": 330.0, "contribution_pct": 0.66},
            "Amazon": {"net_sales": 0.0, "cogs": 0.0, "three_pl": 0.0, "ads": 0.0, "fees": 0.0,
                       "contribution": 0.0, "contribution_pct": None},
            "Wholesale": {"net_sales": 0.0, "cogs": 0.0, "three_pl": 0.0, "ads": 0.0, "fees": 0.0,
                          "contribution": 0.0, "contribution_pct": None},
        },
        "income_statement": {"income": 1500.0, "cogs": 450.0, "expenses": 500.0,
                              "other_income": 0.0, "net_income": 550.0},
    }


def _units_result():
    empty = {"orders": 0, "units": 0, "gross": 0.0, "discounts": 0.0, "refunds": 0.0, "net_revenue": 0.0}
    return {
        "channels": {
            "DTC": {"orders": 20, "units": 40, "gross": 1050.0, "discounts": 50.0, "refunds": 0.0, "net_revenue": 1000.0},
            "TikTok": {"orders": 10, "units": 15, "gross": 520.0, "discounts": 20.0, "refunds": 0.0, "net_revenue": 500.0},
            "Amazon": dict(empty),
            "Wholesale": dict(empty),
        }
    }


def test_dashboard_combines_financials_and_units(monkeypatch):
    monkeypatch.setattr(handlers, "get_channel_financials_live", lambda qbo, s, e: _financials_result())
    monkeypatch.setattr(handlers, "get_channel_units_live", lambda shopify, s, e: _units_result())

    result = handlers.get_dashboard_data(None, None, "2026-08-01", "2026-08-17")

    assert result["company"]["net_sales"] == 1500.0
    assert result["company"]["contribution"] == 910.0
    assert result["company"]["net_income"] == 550.0
    assert result["company"]["overhead"] == 360.0  # contribution 910 - net_income 550
    assert result["channels"]["DTC"]["aov"] == 50.0  # 1000 / 20
    assert result["channels"]["DTC"]["revenue_share"] == 1000.0 / 1500.0
    assert result["channels"]["Amazon"]["aov"] is None  # no orders
    assert "generated_at" in result


def test_dashboard_no_revenue_gives_none_company_pct(monkeypatch):
    empty_financials = {"channels": {ch: {"net_sales": 0.0, "cogs": 0.0, "three_pl": 0.0, "ads": 0.0,
                                           "fees": 0.0, "contribution": 0.0, "contribution_pct": None}
                                      for ch in handlers.CHANNELS},
                         "income_statement": {"income": 0.0, "cogs": 0.0, "expenses": 0.0,
                                               "other_income": 0.0, "net_income": 0.0}}
    empty_units = {"channels": {ch: {"orders": 0, "units": 0, "gross": 0.0, "discounts": 0.0,
                                      "refunds": 0.0, "net_revenue": 0.0} for ch in handlers.CHANNELS}}
    monkeypatch.setattr(handlers, "get_channel_financials_live", lambda qbo, s, e: empty_financials)
    monkeypatch.setattr(handlers, "get_channel_units_live", lambda shopify, s, e: empty_units)

    result = handlers.get_dashboard_data(None, None, "2026-08-01", "2026-08-17")
    assert result["company"]["contribution_pct"] is None


def test_monthly_trend_calls_dashboard_once_per_month_oldest_first(monkeypatch):
    from datetime import date

    calls = []

    def fake_dashboard(qbo, shopify, start, end, include_prior_period=True):
        calls.append((start, end))
        assert include_prior_period is False  # get_monthly_trend must skip the extra QBO pull
        return {"start_date": start, "end_date": end}

    monkeypatch.setattr(handlers, "get_dashboard_data", fake_dashboard)

    result = handlers.get_monthly_trend(None, None, months=3, today=date(2026, 8, 17))

    assert len(result) == 3
    assert calls[0] == ("2026-06-01", "2026-06-30")
    assert calls[1] == ("2026-07-01", "2026-07-31")
    assert calls[2] == ("2026-08-01", "2026-08-17")  # current month, partial - ends today


def test_monthly_trend_crosses_year_boundary_correctly(monkeypatch):
    from datetime import date

    calls = []
    monkeypatch.setattr(
        handlers, "get_dashboard_data",
        lambda qbo, shopify, start, end, include_prior_period=True: calls.append((start, end))
        or {"start_date": start, "end_date": end},
    )

    handlers.get_monthly_trend(None, None, months=3, today=date(2026, 1, 15))

    assert calls[0] == ("2025-11-01", "2025-11-30")
    assert calls[1] == ("2025-12-01", "2025-12-31")
    assert calls[2] == ("2026-01-01", "2026-01-15")


def test_shift_back_one_month_normal_case():
    assert handlers._shift_back_one_month(date(2026, 8, 17)) == date(2026, 7, 17)


def test_shift_back_one_month_january_wraps_to_prior_year():
    assert handlers._shift_back_one_month(date(2026, 1, 15)) == date(2025, 12, 15)


def test_shift_back_one_month_clamps_to_shorter_month():
    # Mar 31 -> Feb has no 31st in 2026 (not a leap year) -> clamp to Feb 28
    assert handlers._shift_back_one_month(date(2026, 3, 31)) == date(2026, 2, 28)


def test_period_comparison_sums_net_sales_and_contribution_only(monkeypatch):
    def fake_financials(qbo, start, end):
        assert start == "2026-07-01" and end == "2026-07-17"  # shifted back one month
        return {
            "channels": {
                "DTC": {"net_sales": 1000.0, "contribution": 300.0, "cogs": 100.0},
                "TikTok": {"net_sales": 500.0, "contribution": -50.0, "cogs": 50.0},
                "Amazon": {"net_sales": 0.0, "contribution": 0.0, "cogs": 0.0},
                "Wholesale": {"net_sales": 0.0, "contribution": 0.0, "cogs": 0.0},
            },
            "income_statement": {"net_income": 125.0},
        }

    monkeypatch.setattr(handlers, "get_channel_financials_live", fake_financials)
    result = handlers.get_period_comparison(None, "2026-08-01", "2026-08-17")

    assert result["start_date"] == "2026-07-01"
    assert result["end_date"] == "2026-07-17"
    assert result["company"]["net_sales"] == 1500.0
    assert result["company"]["contribution"] == 250.0
    assert result["company"]["net_income"] == 125.0
    assert result["channels"]["DTC"] == {"net_sales": 1000.0, "contribution": 300.0}


def test_shift_back_one_year_normal_case():
    assert handlers._shift_back_one_year(date(2026, 8, 18)) == date(2025, 8, 18)


def test_shift_back_one_year_clamps_feb29_in_non_leap_year():
    assert handlers._shift_back_one_year(date(2028, 2, 29)) == date(2027, 2, 28)


def test_prior_comparable_period_month_aligned_shifts_one_month():
    """Month-to-Date (Aug 1-17) and a fully-closed "Last Month" selection both look like this -
    day 1 through some day within the same month."""
    prior_start, prior_end = handlers._prior_comparable_period(date(2026, 8, 1), date(2026, 8, 17))
    assert (prior_start, prior_end) == (date(2026, 7, 1), date(2026, 7, 17))


def test_prior_comparable_period_ytd_shifts_one_year_not_one_month():
    """The actual bug being fixed: Year-to-Date (Jan 1 - Aug 18) must compare to the same
    Jan 1 - Aug 18 span one year earlier, not "one month back" (which would wrongly produce
    Dec 1 - Jul 18, a shorter, differently-shaped, meaningless comparison)."""
    prior_start, prior_end = handlers._prior_comparable_period(date(2026, 1, 1), date(2026, 8, 18))
    assert (prior_start, prior_end) == (date(2025, 1, 1), date(2025, 8, 18))


def test_prior_comparable_period_last_30_days_uses_immediately_preceding_equal_length():
    """Neither month- nor year-aligned - Last 30 Days (or any custom range) compares to the
    immediately preceding period of the same length, ending the day before this one starts."""
    start, end = date(2026, 7, 20), date(2026, 8, 18)  # 30 days
    prior_start, prior_end = handlers._prior_comparable_period(start, end)
    assert prior_end == date(2026, 7, 19)
    assert (prior_end - prior_start).days + 1 == (end - start).days + 1  # same length
    assert prior_start == date(2026, 6, 20)


def test_prior_comparable_period_custom_range_uses_immediately_preceding_equal_length():
    start, end = date(2026, 5, 10), date(2026, 5, 24)  # 15 days, not month-aligned
    prior_start, prior_end = handlers._prior_comparable_period(start, end)
    assert prior_end == date(2026, 5, 9)
    assert prior_start == date(2026, 4, 25)


def test_period_comparison_ytd_range_uses_prior_year_not_prior_month(monkeypatch):
    def fake_financials(qbo, start, end):
        assert start == "2025-01-01" and end == "2025-08-18"
        return {
            "channels": {ch: {"net_sales": 0.0, "contribution": 0.0, "cogs": 0.0} for ch in handlers.CHANNELS},
            "income_statement": {"net_income": 0.0},
        }

    monkeypatch.setattr(handlers, "get_channel_financials_live", fake_financials)
    result = handlers.get_period_comparison(None, "2026-01-01", "2026-08-18")

    assert result["start_date"] == "2025-01-01"
    assert result["end_date"] == "2025-08-18"


def test_dashboard_includes_prior_period_by_default(monkeypatch):
    monkeypatch.setattr(handlers, "get_channel_financials_live", lambda qbo, s, e: _financials_result())
    monkeypatch.setattr(handlers, "get_channel_units_live", lambda shopify, s, e: _units_result())

    result = handlers.get_dashboard_data(None, None, "2026-08-01", "2026-08-17")
    assert "prior_period" in result
    assert result["prior_period"]["start_date"] == "2026-07-01"


def test_dashboard_skips_prior_period_when_disabled(monkeypatch):
    monkeypatch.setattr(handlers, "get_channel_financials_live", lambda qbo, s, e: _financials_result())
    monkeypatch.setattr(handlers, "get_channel_units_live", lambda shopify, s, e: _units_result())

    result = handlers.get_dashboard_data(None, None, "2026-08-01", "2026-08-17", include_prior_period=False)
    assert "prior_period" not in result


def test_sku_units_live_buckets_by_sku_then_channel(monkeypatch):
    orders = [
        {
            "sourceName": "web",  # -> DTC
            "lineItems": {"nodes": [
                {"sku": "OO-OO-ORG-500", "quantity": 3},
                {"sku": "OO-OO-COOK-750ML-SHIP", "quantity": 1},
            ]},
        },
        {
            "sourceName": "tiktok",  # -> TikTok
            "lineItems": {"nodes": [{"sku": "OO-OO-ORG-500", "quantity": 5}]},
        },
        {
            "sourceName": "shopify_draft_order",  # excluded entirely
            "lineItems": {"nodes": [{"sku": "OO-OO-ORG-500", "quantity": 99}]},
        },
        {
            "sourceName": "web",
            "lineItems": {"nodes": [{"sku": None, "quantity": 2}]},  # missing SKU
        },
    ]
    monkeypatch.setattr(handlers.shared_shopify, "fetch_orders", lambda domain, token, start, end: orders)

    fake_shopify = handlers.ShopifyContext("example.myshopify.com", "fake-token")
    result = handlers.get_sku_units_live(fake_shopify, "2026-08-01", "2026-08-17")

    assert result["skus"]["OO-OO-ORG-500"]["name"] == "Nonna's Olive Oil (500mL Original)"
    assert result["skus"]["OO-OO-ORG-500"]["channels"]["DTC"] == 3
    assert result["skus"]["OO-OO-ORG-500"]["channels"]["TikTok"] == 5
    assert result["skus"]["OO-OO-ORG-500"]["units"] == 8  # pack_size 1 - same as raw quantity
    assert result["skus"]["OO-OO-ORG-500"]["product"] == "Nonna's Olive Oil (500mL Original)"
    assert result["skus"]["OO-OO-COOK-750ML-SHIP"]["channels"]["DTC"] == 1
    assert result["skus"]["(no SKU)"]["name"] is None  # not in the registry
    assert result["skus"]["(no SKU)"]["channels"]["DTC"] == 2
    assert result["skus"]["(no SKU)"]["product"] == "(no SKU)"  # falls back to the raw code
    assert len(result["skus"]) == 3  # draft order's SKU never appears


def test_sku_units_live_multiplies_pack_size_into_units(monkeypatch):
    """5 orders of a 3-pack is 15 real bottles, not 5 - the exact scenario a 3-pack SKU exists
    for. "channels" stays the raw Shopify line quantity (5); "units" is the bottle-equivalent."""
    orders = [
        {"sourceName": "amazon", "lineItems": {"nodes": [{"sku": "OO-OO-ORG-501", "quantity": 5}]}},
    ]
    monkeypatch.setattr(handlers.shared_shopify, "fetch_orders", lambda domain, token, start, end: orders)
    monkeypatch.setattr(
        handlers, "load_sku_map",
        lambda: {"OO-OO-ORG-501": {"name": "3-Pack", "product": "Nonna's Olive Oil (500mL Original)", "pack_size": 3, "status": "active"}},
    )

    fake_shopify = handlers.ShopifyContext("example.myshopify.com", "fake-token")
    result = handlers.get_sku_units_live(fake_shopify, "2026-08-01", "2026-08-17")

    assert result["skus"]["OO-OO-ORG-501"]["channels"]["Amazon"] == 5
    assert result["skus"]["OO-OO-ORG-501"]["units"] == 15


def test_sku_units_to_date_combines_reference_and_live(monkeypatch):
    reference = {
        "2024-09": {"DTC": None, "TikTok": None, "Amazon": None, "Wholesale": None, "total_units": 687, "note": ""},
        "2024-10": {"DTC": None, "TikTok": None, "Amazon": None, "Wholesale": None, "total_units": 28, "note": ""},
        "2026-07": {"DTC": 234, "TikTok": 220, "Amazon": 390, "Wholesale": 12, "total_units": 856, "note": ""},
        # Current month's own CSV row must be ignored - the live pull replaces it, since a CSV
        # snapshot can be stale mid-month.
        "2026-08": {"DTC": 5, "TikTok": 0, "Amazon": 0, "Wholesale": 0, "total_units": 5, "note": "stale snapshot"},
    }
    monkeypatch.setattr(handlers, "load_channel_units_by_month", lambda: reference)
    # A second SKU is already registered (active_since 2026-08-14) - every historical month
    # here ends before that date, so it must not affect the historical attribution at all.
    monkeypatch.setattr(
        handlers, "load_sku_map",
        lambda: {
            "OO-OO-ORG-500": {"name": "Nonna's Olive Oil (500mL Original)", "status": "active"},
            "OO-OO-ORG-501": {"name": "3-Pack", "status": "active", "active_since": "2026-08-14"},
        },
    )

    def fake_live(shopify, start, end):
        assert start == "2026-08-01" and end == "2026-08-18"
        return {
            "start_date": start, "end_date": end, "pulled_at": "x",
            "skus": {"OO-OO-ORG-500": {
                "name": "Nonna's Olive Oil (500mL Original)",
                "product": "Nonna's Olive Oil (500mL Original)",
                "pack_size": 1,
                "channels": {"DTC": 55, "TikTok": 24, "Amazon": 90, "Wholesale": 0},
                "units": 169,
            }},
        }
    monkeypatch.setattr(handlers, "get_sku_units_live", fake_live)

    fake_shopify = handlers.ShopifyContext("example.myshopify.com", "fake-token")
    result = handlers.get_sku_units_to_date(fake_shopify, today=date(2026, 8, 18))

    # historical: 687 + 28 + 856 = 1571 (2026-08's own stale row excluded); live: 55+24+90=169
    assert result["skus"]["OO-OO-ORG-500"]["units_to_date"] == 1571 + 169
    assert result["reference_through"] == "2026-07"
    assert result["live_from"] == "2026-08-01"
    assert result["live_to"] == "2026-08-18"


def test_sku_units_to_date_excludes_month_ambiguous_by_its_own_end(monkeypatch):
    """A historical month that ends on/after a second SKU's active_since is genuinely ambiguous
    - excluded rather than guessed at, mirroring compute_sku_revenue's per-transaction-date
    rule. August 2026 is a real example: OO-OO-ORG-501 went active 2026-08-14, so if August ever
    became a "closed" reference month, it would need per-day granularity this CSV doesn't have -
    the safe choice is to drop it, not misattribute it wholesale to either SKU."""
    reference = {
        "2026-08": {"DTC": None, "TikTok": None, "Amazon": None, "Wholesale": None, "total_units": 500, "note": ""},
    }
    monkeypatch.setattr(handlers, "load_channel_units_by_month", lambda: reference)
    monkeypatch.setattr(
        handlers, "load_sku_map",
        lambda: {
            "OO-OO-ORG-500": {"name": "Nonna's Olive Oil (500mL Original)", "status": "active"},
            "OO-OO-ORG-501": {"name": "3-Pack", "status": "active", "active_since": "2026-08-14"},
        },
    )
    monkeypatch.setattr(
        handlers, "get_sku_units_live",
        lambda shopify, start, end: {"start_date": start, "end_date": end, "pulled_at": "x", "skus": {}},
    )

    fake_shopify = handlers.ShopifyContext("example.myshopify.com", "fake-token")
    # today in September so August is a "closed" historical month subject to the fallback check
    result = handlers.get_sku_units_to_date(fake_shopify, today=date(2026, 9, 1))

    assert result["skus"] == {}


def _mock_registry_for_period_tests(handlers, monkeypatch):
    monkeypatch.setattr(
        handlers, "load_sku_map",
        lambda: {"OO-OO-ORG-500": {"name": "Nonna's Olive Oil (500mL Original)", "product": "Nonna's Olive Oil (500mL Original)", "status": "active"}},
    )


def test_sku_units_for_period_pure_live_when_entirely_recent(monkeypatch):
    """A range entirely inside the live-retrievable window never touches the reference file."""
    _mock_registry_for_period_tests(handlers, monkeypatch)
    monkeypatch.setattr(handlers, "load_channel_units_by_month", lambda: {"2026-04": {"total_units": 999, "DTC": None, "TikTok": None, "Amazon": None, "Wholesale": None, "note": ""}})

    def fake_live(shopify, start, end):
        assert start == "2026-08-01" and end == "2026-08-17"
        return {"start_date": start, "end_date": end, "pulled_at": "x", "skus": {
            "OO-OO-ORG-500": {"name": "Nonna's Olive Oil (500mL Original)", "product": "Nonna's Olive Oil (500mL Original)", "pack_size": 1, "channels": {"DTC": 40, "TikTok": 0, "Amazon": 0, "Wholesale": 0}, "units": 40},
        }}
    monkeypatch.setattr(handlers, "get_sku_units_live", fake_live)

    fake_shopify = handlers.ShopifyContext("example.myshopify.com", "fake-token")
    result = handlers.get_sku_units_for_period(fake_shopify, "2026-08-01", "2026-08-17", today=date(2026, 8, 18))

    assert result["skus"]["OO-OO-ORG-500"]["units"] == 40
    assert result["skus"]["OO-OO-ORG-500"]["channels"] == {"DTC": 40, "TikTok": 0, "Amazon": 0, "Wholesale": 0}
    assert result["skus"]["OO-OO-ORG-500"]["pack_size"] == 1
    assert result["reference_months_used"] == []
    assert result["gap_days"] == []


def test_sku_units_for_period_pure_reference_when_whole_month_fully_historical(monkeypatch):
    """A fully historical, month-aligned range never attempts a live pull."""
    _mock_registry_for_period_tests(handlers, monkeypatch)
    monkeypatch.setattr(
        handlers, "load_channel_units_by_month",
        lambda: {"2026-04": {"total_units": 1344, "DTC": 210, "TikTok": 749, "Amazon": 121, "Wholesale": 264, "note": ""}},
    )

    def fake_live(shopify, start, end):
        raise AssertionError("must not pull live for a fully historical range")
    monkeypatch.setattr(handlers, "get_sku_units_live", fake_live)

    fake_shopify = handlers.ShopifyContext("example.myshopify.com", "fake-token")
    result = handlers.get_sku_units_for_period(fake_shopify, "2026-04-01", "2026-04-30", today=date(2026, 8, 18))

    assert result["skus"]["OO-OO-ORG-500"]["units"] == 1344
    assert result["reference_months_used"] == ["2026-04"]
    assert result["live_from"] is None
    assert result["gap_days"] == []


def test_sku_units_for_period_combines_and_reports_gap_when_spanning(monkeypatch):
    """A range spanning the live boundary combines both sources - and a stretch that's neither a
    whole covered reference month nor inside the live pull is reported as a real gap, not
    silently dropped."""
    _mock_registry_for_period_tests(handlers, monkeypatch)
    monkeypatch.setattr(
        handlers, "load_channel_units_by_month",
        lambda: {"2026-04": {"total_units": 1344, "DTC": 210, "TikTok": 749, "Amazon": 121, "Wholesale": 264, "note": ""}},
    )

    def fake_live(shopify, start, end):
        return {"start_date": start, "end_date": end, "pulled_at": "x", "skus": {
            "OO-OO-ORG-500": {"name": "Nonna's Olive Oil (500mL Original)", "product": "Nonna's Olive Oil (500mL Original)", "pack_size": 1, "channels": {"DTC": 10, "TikTok": 0, "Amazon": 0, "Wholesale": 0}, "units": 10},
        }}
    monkeypatch.setattr(handlers, "get_sku_units_live", fake_live)

    fake_shopify = handlers.ShopifyContext("example.myshopify.com", "fake-token")
    # today=2026-08-18 -> live boundary ~2026-06-24 (55 days back). April (whole month, ends well
    # before the boundary) is reference-only; May/June aren't whole-covered reference months
    # (June's own end is on/after the boundary) so they fall into the gap; the live pull only
    # starts at the boundary itself.
    result = handlers.get_sku_units_for_period(fake_shopify, "2026-04-01", "2026-08-17", today=date(2026, 8, 18))

    assert result["reference_months_used"] == ["2026-04"]
    assert result["skus"]["OO-OO-ORG-500"]["units"] == 1344 + 10
    assert result["live_from"] is not None
    assert len(result["gap_days"]) == 1
    gap = result["gap_days"][0]
    assert gap["start"] == "2026-05-01"  # first uncovered day right after April


def test_sku_units_to_date_no_fallback_when_multiple_active(monkeypatch):
    reference = {"2024-09": {"DTC": None, "TikTok": None, "Amazon": None, "Wholesale": None, "total_units": 687, "note": ""}}
    monkeypatch.setattr(handlers, "load_channel_units_by_month", lambda: reference)
    monkeypatch.setattr(
        handlers, "load_sku_map",
        lambda: {
            "OO-OO-ORG-500": {"name": "A", "status": "active"},
            "OO-OO-COOK-750ML-SHIP": {"name": "B", "status": "active"},
        },
    )
    monkeypatch.setattr(
        handlers, "get_sku_units_live",
        lambda shopify, start, end: {"start_date": start, "end_date": end, "pulled_at": "x", "skus": {}},
    )

    fake_shopify = handlers.ShopifyContext("example.myshopify.com", "fake-token")
    result = handlers.get_sku_units_to_date(fake_shopify, today=date(2026, 8, 18))

    # No sole active SKU to safely attribute the 687 historical units to - they're dropped
    # rather than guessed at, same "never guess" principle as the revenue-side fallback.
    assert result["skus"] == {}


def test_sku_revenue_live_uses_shared_qbo_client_and_parser(monkeypatch):
    real_je = {
        "DocNumber": "A2XSH-10Aug-12Aug-281",
        "Line": [
            {
                "Description": "ProductSalesNotTaxed  - OO-OO-ORG-500 - Online store",
                "Amount": 27.0,
                "JournalEntryLineDetail": {
                    "PostingType": "Credit",
                    "AccountRef": {"name": "Product Revenue – DTC"},
                    "ClassRef": {"name": "DTC"},
                },
            },
            {
                "Description": "DiscountNotTaxed  - OO-OO-ORG-500 - Online store",
                "Amount": 3.24,
                "JournalEntryLineDetail": {
                    "PostingType": "Debit",
                    "AccountRef": {"name": "Discounts & Promotions"},
                    "ClassRef": {"name": "DTC"},
                },
            },
        ]
    }
    calls = []

    def fake_fetch(realm_id, access_token, start, end, environment="production"):
        calls.append((start, end))
        return [real_je]

    monkeypatch.setattr(handlers.shared_qbo, "fetch_journal_entries", fake_fetch)

    fake_qbo = handlers.QboContext("fake-token", "fake-realm", "production")
    result = handlers.get_sku_revenue_live(fake_qbo, "2026-08-10", "2026-08-12")

    assert calls[0] == (date(2026, 8, 10), date(2026, 8, 12))
    assert result["skus"]["OO-OO-ORG-500"]["revenue"] == 27.0
    assert result["skus"]["OO-OO-ORG-500"]["discounts"] == -3.24
    assert round(result["skus"]["OO-OO-ORG-500"]["net"], 2) == 23.76
    assert result["journal_entries_scanned"] == 1


def test_estimate_next_payroll_returns_none_with_fewer_than_two_transactions(monkeypatch):
    monkeypatch.setattr(
        handlers.shared_qbo, "fetch_gl_account_transactions",
        lambda *a, **k: [{"date": "2026-08-13", "amount": 5391.65}],
    )
    fake_qbo = handlers.QboContext("fake-token", "fake-realm", "production")
    assert handlers._estimate_next_payroll(fake_qbo, date(2026, 8, 18)) is None


def test_estimate_next_payroll_projects_from_last_interval(monkeypatch):
    # Mirrors real data confirmed live 2026-08-18: flat biweekly Founder/Officer Compensation.
    monkeypatch.setattr(
        handlers.shared_qbo, "fetch_gl_account_transactions",
        lambda *a, **k: [
            {"date": "2026-07-16", "amount": 5391.65},
            {"date": "2026-07-30", "amount": 5391.65},
            {"date": "2026-08-13", "amount": 5391.65},
        ],
    )
    fake_qbo = handlers.QboContext("fake-token", "fake-realm", "production")
    result = handlers._estimate_next_payroll(fake_qbo, date(2026, 8, 18))
    assert result == {
        "amount": 5391.65, "last_date": "2026-08-13", "cadence_days": 14,
        "next_estimated_date": "2026-08-27",
    }


def test_get_cash_snapshot_computes_cash_basis_burn_and_runway_not_net_income(monkeypatch):
    """The whole point of this tab: burn/runway come from the actual change in cash balance,
    not from accrual Net Income - this test uses deliberately different values for the two so a
    regression that swaps one for the other would fail loudly."""
    bank_prefixes = ["11100 Chase Operating Bank Account"]
    monkeypatch.setattr(
        handlers, "load_qbo_account_map",
        lambda: {"bank_cash": bank_prefixes, "financing": {"accounts": []}},
    )
    today = date(2026, 8, 18)
    lookback_start = today - timedelta(days=90)
    # Cumulative balance is 9900.0 through lookback_start, then a 900.0 outflow lands inside the
    # 90-day window - so balance_as_of(lookback_start)=9900.0, balance_as_of(today)=9000.0.
    bank_txns = [
        {"date": "2020-01-01", "amount": 9900.0},
        {"date": (lookback_start + timedelta(days=5)).isoformat(), "amount": -900.0},
    ]

    def fake_multi(realm_id, access_token, prefixes, start, end, environment="production"):
        return bank_txns if prefixes == bank_prefixes else []

    monkeypatch.setattr(handlers.shared_qbo, "fetch_gl_account_transactions_multi", fake_multi)
    monkeypatch.setattr(
        handlers.shared_qbo, "fetch_profit_and_loss_by_class",
        lambda realm_id, access_token, start, end, environment="production": {
            "Income": {"41100 Product Revenue": {"Total": 1000.0}},
            "Expenses": {"84100 Legal Fees": {"Total": 2000.0}},
        },
    )
    monkeypatch.setattr(
        handlers.shared_qbo, "fetch_gl_account_transactions",
        lambda *a, **k: [
            {"date": "2026-07-30", "amount": 5391.65},
            {"date": "2026-08-13", "amount": 5391.65},
        ],
    )

    fake_qbo = handlers.QboContext("fake-token", "fake-realm", "production")
    result = handlers.get_cash_snapshot(fake_qbo, today=today)

    assert result["cash_balance"] == 9000.0
    assert result["cash_balance_90d_ago"] == 9900.0
    assert result["net_cash_change_90d"] == -900.0
    assert result["financing_inflows_90d"] == 0.0
    assert result["operating_cash_change_90d"] == -900.0
    assert result["monthly_burn"] == 300.0
    assert result["runway_months"] == 30.0
    assert result["net_income_90d"] == -1000.0  # 1000 income - 2000 expenses - deliberately
    # different from the cash-basis numbers above, confirming they're computed independently
    assert result["overhead_accounts"] == [{"label": "84100 Legal Fees", "monthly_avg": 2000.0 / 3}]
    assert result["overhead_monthly_total"] == 2000.0 / 3
    assert len(result["balance_trend"]) == 6
    assert result["known_payroll"]["amount"] == 5391.65
    assert result["known_payroll"]["cadence_days"] == 14


def test_get_cash_snapshot_excludes_financing_inflows_from_burn(monkeypatch):
    """The reason this tab exists in its current form: a raw balance-change burn calc gets
    diluted by equity/SAFE investment inflows, making the business look healthier than it is.
    financing_inflows_90d must be subtracted before computing monthly_burn/runway_months."""
    bank_prefixes = ["11100 Chase Operating Bank Account"]
    financing_prefixes = ["34000 Additional Paid In Capital"]
    today = date(2026, 8, 18)
    lookback_start = today - timedelta(days=90)
    # Combined balance only fell $900 over 90 days, but that includes a $5,000 SAFE investment -
    # true operating cash change is -$5,900, not -$900.
    bank_txns = [
        {"date": "2020-01-01", "amount": 9900.0},
        {"date": (lookback_start + timedelta(days=5)).isoformat(), "amount": -900.0},
    ]
    financing_txns = [{"date": (lookback_start + timedelta(days=10)).isoformat(), "amount": 5000.0}]

    def fake_multi(realm_id, access_token, prefixes, start, end, environment="production"):
        if prefixes == bank_prefixes:
            return bank_txns
        if prefixes == financing_prefixes:
            return financing_txns
        return []

    monkeypatch.setattr(
        handlers, "load_qbo_account_map",
        lambda: {"bank_cash": bank_prefixes, "financing": {"accounts": financing_prefixes}},
    )
    monkeypatch.setattr(handlers.shared_qbo, "fetch_gl_account_transactions_multi", fake_multi)
    monkeypatch.setattr(
        handlers.shared_qbo, "fetch_profit_and_loss_by_class",
        lambda realm_id, access_token, start, end, environment="production": {},
    )
    monkeypatch.setattr(handlers.shared_qbo, "fetch_gl_account_transactions", lambda *a, **k: [])

    fake_qbo = handlers.QboContext("fake-token", "fake-realm", "production")
    result = handlers.get_cash_snapshot(fake_qbo, today=today)

    assert result["net_cash_change_90d"] == -900.0
    assert result["financing_inflows_90d"] == 5000.0
    assert result["operating_cash_change_90d"] == -5900.0
    assert result["monthly_burn"] == 5900.0 / 3
    assert result["runway_months"] == 9000.0 / (5900.0 / 3)


def test_get_cash_snapshot_runway_is_none_when_not_burning_cash(monkeypatch):
    bank_prefixes = ["11100 Chase Operating Bank Account"]
    monkeypatch.setattr(
        handlers, "load_qbo_account_map",
        lambda: {"bank_cash": bank_prefixes, "financing": {"accounts": []}},
    )
    today = date(2026, 8, 18)
    lookback_start = today - timedelta(days=90)
    # Cash grew, not shrank, over the trailing 90 days.
    bank_txns = [
        {"date": "2020-01-01", "amount": 9000.0},
        {"date": (lookback_start + timedelta(days=5)).isoformat(), "amount": 1000.0},
    ]

    def fake_multi(realm_id, access_token, prefixes, start, end, environment="production"):
        return bank_txns if prefixes == bank_prefixes else []

    monkeypatch.setattr(handlers.shared_qbo, "fetch_gl_account_transactions_multi", fake_multi)
    monkeypatch.setattr(
        handlers.shared_qbo, "fetch_profit_and_loss_by_class",
        lambda realm_id, access_token, start, end, environment="production": {},
    )
    monkeypatch.setattr(handlers.shared_qbo, "fetch_gl_account_transactions", lambda *a, **k: [])

    fake_qbo = handlers.QboContext("fake-token", "fake-realm", "production")
    result = handlers.get_cash_snapshot(fake_qbo, today=today)

    assert result["monthly_burn"] < 0  # negative burn = growing cash
    assert result["runway_months"] is None
    assert result["known_payroll"] is None
