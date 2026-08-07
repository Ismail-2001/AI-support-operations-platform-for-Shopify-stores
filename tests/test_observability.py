"""Tests for agent/observability.py and the storage-level trace/cost/calibration methods.
Also covers the highest-stakes new behavior: a daily cost cap breach must force auto-send
off, even when everything else about a ticket would qualify for auto-send."""

import pytest

from agent.config import settings
from agent.observability import _compute_cost, _extract_usage, check_daily_cost_cap, record_llm_call

pytestmark = pytest.mark.asyncio


class _FakeUsageMessage:
    def __init__(self, input_tokens, output_tokens):
        self.usage_metadata = {"input_tokens": input_tokens, "output_tokens": output_tokens}


def test_extract_usage_from_raw_message():
    msg = _FakeUsageMessage(100, 50)
    tokens_in, tokens_out = _extract_usage(msg)
    assert tokens_in == 100
    assert tokens_out == 50


def test_extract_usage_handles_none_message():
    assert _extract_usage(None) == (0, 0)


def test_extract_usage_handles_missing_usage_metadata():
    class NoUsage:
        pass

    assert _extract_usage(NoUsage()) == (0, 0)


def test_compute_cost_uses_correct_pricing():
    # gemini-2.0-flash: $0.075/1M input, $0.30/1M output
    cost = _compute_cost(tokens_in=1_000_000, tokens_out=1_000_000, model="gemini-2.0-flash")
    assert cost == pytest.approx(0.375, abs=0.001)


def test_compute_cost_falls_back_to_default_pricing_for_unknown_model():
    cost = _compute_cost(tokens_in=1_000_000, tokens_out=0, model="some-unknown-model")
    assert cost == pytest.approx(0.075, abs=0.001)  # falls back to gemini-2.0-flash pricing


async def test_record_llm_call_persists_trace_and_cost(test_store):
    obs_module_store_backup = None
    import agent.storage as storage_module

    storage_module.store = test_store

    msg = _FakeUsageMessage(200, 100)
    cost = await record_llm_call(
        ticket_id="t1",
        stage="classification",
        model="gemini-2.0-flash",
        raw_message=msg,
        latency_ms=123.4,
        input_summary={"transcript": "hello"},
        output_summary={"category": "shipping"},
    )
    assert cost > 0

    traces = await test_store.get_traces("t1")
    assert len(traces) == 1
    assert traces[0]["stage"] == "classification"
    assert traces[0]["tokens_input"] == 200
    assert traces[0]["tokens_output"] == 100
    assert traces[0]["latency_ms"] == 123.4

    today_cost = await test_store.get_today_cost_usd()
    assert today_cost == cost


async def test_record_llm_call_with_zero_tokens_does_not_pollute_cost_table(test_store):
    import agent.storage as storage_module

    storage_module.store = test_store

    await record_llm_call(
        ticket_id="t2",
        stage="classification",
        model="gemini-2.0-flash",
        raw_message=None,
        latency_ms=10.0,
        input_summary={},
        output_summary={},
    )
    # trace should still be recorded (for debugging), but no cost row for a zero-cost call
    traces = await test_store.get_traces("t2")
    assert len(traces) == 1
    report = await test_store.get_cost_report()
    assert report["today_usd"] == 0.0


async def test_daily_cost_cap_disabled_when_set_to_zero(test_store, monkeypatch):
    import agent.storage as storage_module

    storage_module.store = test_store
    settings.DAILY_COST_CAP_USD = 0
    assert await check_daily_cost_cap() is True


async def test_daily_cost_cap_allows_when_under_budget(test_store):
    import agent.storage as storage_module

    storage_module.store = test_store
    settings.DAILY_COST_CAP_USD = 5.0
    await test_store.record_cost("t1", "classification", "gemini-2.0-flash", 100, 50, cost_usd=0.01)
    assert await check_daily_cost_cap() is True


async def test_daily_cost_cap_blocks_when_over_budget(test_store):
    import agent.storage as storage_module

    storage_module.store = test_store
    settings.DAILY_COST_CAP_USD = 0.01
    await test_store.record_cost(
        "t1", "classification", "gemini-2.0-flash", 1_000_000, 1_000_000, cost_usd=0.5
    )
    assert await check_daily_cost_cap() is False


async def test_cost_cap_breach_forces_auto_send_off_end_to_end(test_store):
    """The highest-stakes test in this file: even a ticket that would otherwise qualify
    for auto-send (high confidence, allowed category, no suggested_action) must NOT
    auto-send once today's spend has crossed DAILY_COST_CAP_USD."""
    import agent.storage as storage_module
    import agent.support_agent as sa
    from agent.models import SupportTicket, TicketCategory
    from agent.support_agent import CustomerSupportAgent
    from tests.conftest import FakeClassifier, FakeResponseEngine, FakeShopify

    storage_module.store = test_store
    sa.store = test_store

    settings.AUTO_SEND_ENABLED = True
    settings.AUTO_SEND_MIN_CONFIDENCE = 0.85
    settings.DAILY_COST_CAP_USD = 0.01

    # Blow past the cap before this ticket is even processed
    await test_store.record_cost(
        "prior", "classification", "gemini-2.0-flash", 1_000_000, 1_000_000, cost_usd=1.0
    )

    agent = CustomerSupportAgent.__new__(CustomerSupportAgent)
    agent.classifier = FakeClassifier(category=TicketCategory.SHIPPING)
    agent.response_engine = FakeResponseEngine(confidence=0.95, requires_human_review=False)
    agent.shopify = FakeShopify()

    ticket = SupportTicket(
        id="capped1", customer_email="a@b.com", subject="Q", body="when will it arrive"
    )
    decision = await agent.handle_ticket(ticket)

    assert (
        decision.auto_sent is False
    ), "auto-send must be blocked once the daily cost cap is exceeded"
    assert (
        decision.suggestion.confidence == 0.95
    ), "the ticket should still be classified/drafted normally"
