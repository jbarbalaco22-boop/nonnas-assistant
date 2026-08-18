"""Tests for tool schema structure and dispatch — not the live API calls inside the handlers
themselves, which were verified directly against real QBO/Shopify data instead of mocked."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools import TOOL_SCHEMAS, dispatch  # noqa: E402


def test_every_tool_has_required_schema_fields():
    for tool in TOOL_SCHEMAS:
        assert "name" in tool
        assert "description" in tool
        assert tool["description"], f"{tool['name']} has an empty description"
        assert "input_schema" in tool
        assert tool["input_schema"]["type"] == "object"


def test_tool_names_are_unique():
    names = [t["name"] for t in TOOL_SCHEMAS]
    assert len(names) == len(set(names))


def test_dispatch_unknown_tool_raises():
    try:
        dispatch("not_a_real_tool", {}, qbo_context=None, shopify_context=None)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_live_tools_require_date_range():
    financials_schema = next(t for t in TOOL_SCHEMAS if t["name"] == "get_channel_financials_live")
    assert set(financials_schema["input_schema"]["required"]) == {"start_date", "end_date"}

    units_schema = next(t for t in TOOL_SCHEMAS if t["name"] == "get_channel_units_live")
    assert set(units_schema["input_schema"]["required"]) == {"start_date", "end_date"}

    sku_units_schema = next(t for t in TOOL_SCHEMAS if t["name"] == "get_sku_units_live")
    assert set(sku_units_schema["input_schema"]["required"]) == {"start_date", "end_date"}


def test_channel_enum_matches_the_four_real_channels():
    financials_schema = next(t for t in TOOL_SCHEMAS if t["name"] == "get_channel_financials_live")
    assert set(financials_schema["input_schema"]["properties"]["channel"]["enum"]) == {
        "DTC", "TikTok", "Amazon", "Wholesale"
    }


def test_get_unit_reference_dispatches_and_returns_real_data():
    """No mocking - this reads the actual packaged CSV via nonnas_shared, same as production."""
    result = dispatch("get_unit_reference", {}, qbo_context=None, shopify_context=None)
    assert "months" in result
    assert "2026-07" in result["months"]
    assert result["months"]["2026-07"]["DTC"] == 234
