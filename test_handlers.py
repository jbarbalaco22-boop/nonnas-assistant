"""Tests for get_dashboard_data's combination logic — with get_channel_financials_live and
get_channel_units_live mocked, so this doesn't hit real QBO/Shopify APIs. The combination itself
(company totals, per-channel health metrics, revenue concentration) was also verified directly
against real July 2026 data, separately from these unit tests."""
import sys
from datetime import date
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
            }
        }

    monkeypatch.setattr(handlers, "get_channel_financials_live", fake_financials)
    result = handlers.get_period_comparison(None, "2026-08-01", "2026-08-17")

    assert result["start_date"] == "2026-07-01"
    assert result["end_date"] == "2026-07-17"
    assert result["company"]["net_sales"] == 1500.0
    assert result["company"]["contribution"] == 250.0
    assert result["channels"]["DTC"] == {"net_sales": 1000.0, "contribution": 300.0}


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
    assert result["skus"]["OO-OO-COOK-750ML-SHIP"]["channels"]["DTC"] == 1
    assert result["skus"]["(no SKU)"]["name"] is None  # not in the registry
    assert result["skus"]["(no SKU)"]["channels"]["DTC"] == 2
    assert len(result["skus"]) == 3  # draft order's SKU never appears


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
