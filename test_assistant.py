"""Tests for the ask() loop's safety cap and caching setup — using a fake Anthropic client so
this doesn't need a real API key or live QBO/Shopify credentials to verify the loop logic itself.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "nonnas-shared"))

os.environ.setdefault("QBO_CLIENT_ID", "test")
os.environ.setdefault("QBO_CLIENT_SECRET", "test")
os.environ.setdefault("QBO_REFRESH_TOKEN", "test")
os.environ.setdefault("QBO_REALM_ID", "test")
os.environ.setdefault("SHOPIFY_STORE_DOMAIN", "test.myshopify.com")
os.environ.setdefault("SHOPIFY_CLIENT_ID", "test")
os.environ.setdefault("SHOPIFY_CLIENT_SECRET", "test")

import assistant  # noqa: E402


class _FakeToolUseBlock:
    type = "tool_use"
    id = "toolu_fake"
    name = "get_daily_snapshot"
    input = {}


class _FakeTextBlock:
    type = "text"
    text = "Here's the answer."


class _FakeResponse:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content


class _AlwaysToolUseClient:
    """Simulates a model that never stops calling tools — the runaway-loop scenario."""

    def __init__(self):
        self.call_count = 0
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.call_count += 1
        return _FakeResponse("tool_use", [_FakeToolUseBlock()])


class _AnswersImmediatelyClient:
    def __init__(self):
        self.call_count = 0
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.call_count += 1
        return _FakeResponse("end_turn", [_FakeTextBlock()])


def _no_op_dispatch(*args, **kwargs):
    return {"ok": True}


def test_loop_stops_at_max_iterations_instead_of_running_forever(monkeypatch):
    monkeypatch.setattr(assistant, "dispatch", _no_op_dispatch)
    monkeypatch.setattr(assistant, "_get_qbo_context", lambda: None)
    monkeypatch.setattr(assistant, "_get_shopify_context", lambda: None)

    fake_client = _AlwaysToolUseClient()
    result = assistant.ask("does this ever stop?", client=fake_client)

    assert fake_client.call_count == assistant.MAX_TOOL_ITERATIONS
    assert "wasn't able to finish" in result


def test_loop_returns_immediately_when_model_just_answers(monkeypatch):
    monkeypatch.setattr(assistant, "_get_qbo_context", lambda: None)
    monkeypatch.setattr(assistant, "_get_shopify_context", lambda: None)

    fake_client = _AnswersImmediatelyClient()
    result = assistant.ask("simple question", client=fake_client)

    assert fake_client.call_count == 1
    assert result == "Here's the answer."


def test_system_prompt_and_tools_have_cache_breakpoints():
    assert assistant.CACHED_SYSTEM[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert assistant.CACHED_TOOLS[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    # only the last tool needs the breakpoint - caching applies to everything up to that point
    assert "cache_control" not in assistant.CACHED_TOOLS[0]


class _RecordingClient:
    """Captures the `messages` list passed to the (single) create() call, so a test can inspect
    exactly what the model saw."""

    def __init__(self):
        self.call_count = 0
        self.last_messages = None
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.call_count += 1
        # Copy, not a reference - ask() mutates the same list object (appends the assistant's
        # reply) right after this call returns, which would otherwise silently shift the
        # snapshot a caller inspects afterward.
        self.last_messages = list(kwargs["messages"])
        return _FakeResponse("end_turn", [_FakeTextBlock()])


def test_selected_range_prepended_as_context_note(monkeypatch):
    """Regression for a real audit finding (2026-08-18): with Year-to-Date selected on screen,
    a question like "what's driving this period" got answered about July vs. August MTD instead
    - no connection to what was actually selected. selected_range should ground "this period"-
    style phrasing against the dashboard's current filter."""
    monkeypatch.setattr(assistant, "_get_qbo_context", lambda: None)
    monkeypatch.setattr(assistant, "_get_shopify_context", lambda: None)

    fake_client = _RecordingClient()
    assistant.ask(
        "what's driving the change this period?", client=fake_client,
        selected_range=("2026-01-01", "2026-08-18"),
    )

    sent_content = fake_client.last_messages[-1]["content"]
    assert "2026-01-01" in sent_content
    assert "2026-08-18" in sent_content
    assert "what's driving the change this period?" in sent_content


def test_no_selected_range_leaves_question_unmodified(monkeypatch):
    monkeypatch.setattr(assistant, "_get_qbo_context", lambda: None)
    monkeypatch.setattr(assistant, "_get_shopify_context", lambda: None)

    fake_client = _RecordingClient()
    assistant.ask("simple question", client=fake_client)

    assert fake_client.last_messages[-1]["content"] == "simple question"
