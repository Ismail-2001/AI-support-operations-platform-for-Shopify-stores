"""Auto-send calibration: per-category thresholds, runtime overrides, recommendations.

Three layers:
1. Pure functions (suggest_threshold) — table-style unit tests.
2. Resolution logic (get_min_confidence) — env default vs runtime override vs floor.
3. Graph behavior — a ticket below its category's floor must NOT auto-send, and
   lowering the floor via the API must take effect on the very next ticket.
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent.rate_limit as rate_limit_module
import agent.storage as storage_module
from agent.automation import (
    ABSOLUTE_MIN_THRESHOLD,
    AUTO_SENDABLE_CATEGORIES,
    get_min_confidence,
    suggest_threshold,
)
from agent.config import settings
from agent.models import Sentiment, TicketCategory, TicketPriority
from tests.conftest import FakeClassifier, FakeResponseEngine, FakeShopify

# ── suggest_threshold (pure) ────────────────────────────────


def test_suggest_threshold_needs_fifty_samples():
    samples = [(0.9, False)] * 49
    suggested, reason = suggest_threshold(0.88, samples)
    assert suggested == 0.88
    assert "Not enough data" in reason
    assert "49/50" in reason


def test_suggest_threshold_raises_when_high_confidence_drafts_still_edited():
    # 15 samples in the 0.9+ bucket, 4 of them edited (27% > 10%)
    samples = [(0.95, True)] * 4 + [(0.95, False)] * 11 + [(0.6, False)] * 40
    suggested, reason = suggest_threshold(0.88, samples)
    assert suggested == 1.0, "must raise above the badly-calibrated bucket"
    assert "Raise" in reason


def test_suggest_threshold_lowers_when_bucket_below_never_edited():
    # current floor 0.90; bucket 0.85-0.90 has 20 clean samples → suggest 0.85
    samples = [(0.87, False)] * 20 + [(0.95, False)] * 30
    suggested, reason = suggest_threshold(0.90, samples)
    assert suggested == 0.85
    assert "Lower" in reason


def test_suggest_threshold_never_lowers_below_absolute_floor():
    # bucket below current is clean but its floor is under ABSOLUTE_MIN_THRESHOLD
    samples = [(0.81, False)] * 20 + [(0.95, False)] * 30
    suggested, _ = suggest_threshold(0.86, samples)
    assert suggested >= ABSOLUTE_MIN_THRESHOLD


def test_suggest_threshold_keeps_current_when_healthy():
    # clean samples above and below — nothing to fix
    samples = [(0.95, False)] * 30 + [(0.86, False)] * 10 + [(0.6, True)] * 15
    suggested, reason = suggest_threshold(0.88, samples)
    assert suggested == 0.88
    assert "Keep" in reason


# ── get_min_confidence resolution ───────────────────────────


async def test_get_min_confidence_uses_category_settings_default(storage_ready):
    assert await get_min_confidence("order_status") == 0.88
    assert await get_min_confidence("returns") == 0.87
    assert await get_min_confidence("product_question") == 0.90


async def test_get_min_confidence_runtime_override_wins(storage_ready):
    from agent.automation import set_min_confidence

    await set_min_confidence("shipping", 0.95)
    assert await get_min_confidence("shipping") == 0.95


async def test_get_min_confidence_override_floored_at_absolute_min(storage_ready):
    from agent.automation import _kv_set, threshold_kv_key

    await _kv_set(threshold_kv_key("shipping"), "0.5")
    assert await get_min_confidence("shipping") == ABSOLUTE_MIN_THRESHOLD


async def test_set_min_confidence_rejects_blocked_category(storage_ready):
    from agent.automation import set_min_confidence

    with pytest.raises(ValueError, match="not auto-sendable"):
        await set_min_confidence("refund", 0.9)


async def test_set_min_confidence_rejects_out_of_range(storage_ready):
    from agent.automation import set_min_confidence

    with pytest.raises(ValueError, match="threshold must be between"):
        await set_min_confidence("shipping", 0.3)


async def test_get_min_confidence_survives_missing_table(tmp_path):
    """store.init() never ran → app_settings doesn't exist → fall back to settings."""
    from agent.storage import TicketStore

    original = storage_module.store
    storage_module.store = TicketStore(db_path=str(tmp_path / "uninitialized.db"))
    try:
        assert await get_min_confidence("order_status") == 0.88
    finally:
        storage_module.store = original


@pytest.fixture
async def storage_ready(tmp_path):
    """Initialized store (temp DB) swapped in as the singleton — mirrors what _wire_agent
    does in test_support_agent so KV reads/writes land in the temp file."""
    from agent.storage import TicketStore

    s = TicketStore(db_path=str(tmp_path / "auto.db"))
    await s.init()
    original = storage_module.store
    storage_module.store = s
    yield s
    storage_module.store = original


# ── Graph behavior with per-category thresholds ─────────────


def _wire(test_store, category=TicketCategory.ORDER_STATUS, confidence=0.86):
    import agent.support_agent as sa

    storage_module.store = test_store
    sa.store = test_store
    from agent.support_agent import CustomerSupportAgent

    agent = CustomerSupportAgent.__new__(CustomerSupportAgent)
    agent.classifier = FakeClassifier(
        category=category, priority=TicketPriority.NORMAL, sentiment=Sentiment.NEUTRAL
    )
    agent.response_engine = FakeResponseEngine(confidence=confidence, requires_human_review=False)
    agent.shopify = FakeShopify()
    return agent


async def test_ticket_below_category_threshold_does_not_auto_send(test_store):
    from agent.models import SupportTicket

    settings.AUTO_SEND_ENABLED = True
    # order_status floor is 0.88 — 0.86 is above the legacy global (0.85) but below
    # the category floor, which is exactly the regression this guard exists for.
    agent = _wire(test_store, category=TicketCategory.ORDER_STATUS, confidence=0.86)
    ticket = SupportTicket(id="thr1", customer_email="a@b.com", subject="Q", body="where is 1042")
    decision = await agent.handle_ticket(ticket)
    assert decision.auto_sent is False


async def test_ticket_meeting_category_threshold_auto_sends(test_store):
    from agent.models import SupportTicket

    settings.AUTO_SEND_ENABLED = True
    agent = _wire(test_store, category=TicketCategory.ORDER_STATUS, confidence=0.92)
    ticket = SupportTicket(id="thr2", customer_email="a@b.com", subject="Q", body="where is 1042")
    decision = await agent.handle_ticket(ticket)
    assert decision.auto_sent is True


async def test_runtime_override_applies_to_next_ticket(test_store):
    """The whole point of the KV override: PUT the threshold, next ticket behaves differently."""
    from agent.automation import set_min_confidence
    from agent.models import SupportTicket

    settings.AUTO_SEND_ENABLED = True
    agent = _wire(test_store, category=TicketCategory.ORDER_STATUS, confidence=0.86)

    decision = await agent.handle_ticket(
        SupportTicket(id="thr3", customer_email="a@b.com", subject="Q", body="hi")
    )
    assert decision.auto_sent is False, "0.86 < 0.88 default floor"

    await set_min_confidence("order_status", 0.80)
    decision = await agent.handle_ticket(
        SupportTicket(id="thr4", customer_email="a@b.com", subject="Q", body="hi")
    )
    assert decision.auto_sent is True, "override 0.80 must apply without restart"


# ── API endpoints ───────────────────────────────────────────


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-key-for-tests")
    monkeypatch.setenv("TENANT_NAME", "test")
    settings.GOOGLE_API_KEY = SecretStr("dummy-key-for-tests")
    settings.DB_PATH = str(tmp_path / "test_auto.db")
    storage_module.store.db_path = settings.DB_PATH
    settings.REQUIRE_API_KEY = True
    settings.API_KEY = SecretStr("test-key-123")
    settings.RATE_LIMIT_PER_MINUTE = 100
    rate_limit_module._request_log.clear()

    import importlib

    import api.customer_support as cs_module
    import api.main as main_module

    importlib.reload(cs_module)
    importlib.reload(main_module)

    cs_module._agent.classifier = FakeClassifier()
    cs_module._agent.response_engine = FakeResponseEngine()
    cs_module._agent.shopify = FakeShopify()

    with TestClient(main_module.app, raise_server_exceptions=False) as c:
        yield c


AUTH = {"X-API-Key": "test-key-123"}


def test_thresholds_endpoint_lists_all_sendable_categories(api_client):
    r = api_client.get("/support/automation/thresholds", headers=AUTH)
    assert r.status_code == 200
    thresholds = r.json()["thresholds"]
    assert {t["category"] for t in thresholds} == set(AUTO_SENDABLE_CATEGORIES)
    order_status = next(t for t in thresholds if t["category"] == "order_status")
    assert order_status["min_confidence"] == 0.88
    assert order_status["source"] == "settings"


def test_put_threshold_sets_runtime_override(api_client):
    r = api_client.put(
        "/support/automation/thresholds",
        headers=AUTH,
        json={"category": "shipping", "min_confidence": 0.93},
    )
    assert r.status_code == 200
    assert r.json()["min_confidence"] == 0.93

    r2 = api_client.get("/support/automation/thresholds", headers=AUTH)
    shipping = next(t for t in r2.json()["thresholds"] if t["category"] == "shipping")
    assert shipping["min_confidence"] == 0.93
    assert shipping["source"] == "runtime_override"


def test_put_threshold_null_clears_override(api_client):
    api_client.put(
        "/support/automation/thresholds",
        headers=AUTH,
        json={"category": "returns", "min_confidence": 0.95},
    )
    r = api_client.put(
        "/support/automation/thresholds",
        headers=AUTH,
        json={"category": "returns", "min_confidence": None},
    )
    assert r.status_code == 200
    assert r.json()["min_confidence"] == 0.87  # back to .env default


def test_put_threshold_rejects_blocked_category(api_client):
    r = api_client.put(
        "/support/automation/thresholds",
        headers=AUTH,
        json={"category": "refund", "min_confidence": 0.5},
    )
    assert r.status_code == 422


def test_put_threshold_rejects_below_absolute_floor(api_client):
    r = api_client.put(
        "/support/automation/thresholds",
        headers=AUTH,
        json={"category": "shipping", "min_confidence": 0.3},
    )
    assert r.status_code == 422


def test_auto_send_analytics_reports_current_vs_suggested(api_client):
    r = api_client.get("/support/analytics/auto-send", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert set(body["categories"]) == set(AUTO_SENDABLE_CATEGORIES)
    assert "refund" in body["blocked_categories"]
    entry = body["categories"]["order_status"]
    assert entry["current_threshold"] == 0.88
    assert entry["suggested_threshold"] == 0.88
    assert "Not enough data" in entry["recommendation"]
    assert body["min_samples_for_recommendation"] == 50
    assert body["daily_cost_cap_usd"] == settings.DAILY_COST_CAP_USD


def test_auto_send_analytics_recommends_after_fifty_samples(api_client):
    """Seed 60 reviewed drafts where 0.9+ confidence is still edited 30% of the time —
    the report must recommend raising, with the reason spelled out."""
    import asyncio

    async def seed():
        for i in range(18):
            await storage_module.store.log_edit(
                f"t{i}",
                "ai draft",
                "human rewrote it",
                category="order_status",
                confidence=0.95,
            )
        for i in range(42):
            await storage_module.store.log_edit(
                f"u{i}",
                "ai draft",
                "ai draft",  # identical → not edited
                category="order_status",
                confidence=0.70,
            )

    asyncio.run(seed())
    r = api_client.get("/support/analytics/auto-send", headers=AUTH)
    entry = r.json()["categories"]["order_status"]
    assert entry["reviewed_samples"] == 60
    assert entry["suggested_threshold"] == 1.0
    assert "Raise" in entry["recommendation"]
