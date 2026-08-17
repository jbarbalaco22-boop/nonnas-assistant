"""Tests for get_dashboard_data's combination logic — with get_channel_financials_live and
get_channel_units_live mocked, so this doesn't hit real QBO/Shopify APIs. The combination itself
(company totals, per-channel health metrics, revenue concentration) was also verified directly
against real July 2026 data, separately from these unit tests."""
import sys
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
        }
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
    assert result["channels"]["DTC"]["aov"] == 50.0  # 1000 / 20
    assert result["channels"]["DTC"]["revenue_share"] == 1000.0 / 1500.0
    assert result["channels"]["Amazon"]["aov"] is None  # no orders
    assert "generated_at" in result


def test_dashboard_no_revenue_gives_none_company_pct(monkeypatch):
    empty_financials = {"channels": {ch: {"net_sales": 0.0, "cogs": 0.0, "three_pl": 0.0, "ads": 0.0,
                                           "fees": 0.0, "contribution": 0.0, "contribution_pct": None}
                                      for ch in handlers.CHANNELS}}
    empty_units = {"channels": {ch: {"orders": 0, "units": 0, "gross": 0.0, "discounts": 0.0,
                                      "refunds": 0.0, "net_revenue": 0.0} for ch in handlers.CHANNELS}}
    monkeypatch.setattr(handlers, "get_channel_financials_live", lambda qbo, s, e: empty_financials)
    monkeypatch.setattr(handlers, "get_channel_units_live", lambda shopify, s, e: empty_units)

    result = handlers.get_dashboard_data(None, None, "2026-08-01", "2026-08-17")
    assert result["company"]["contribution_pct"] is None
