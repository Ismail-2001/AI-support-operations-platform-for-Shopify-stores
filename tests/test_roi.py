"""ROI / impact dashboard tests: the pure math (compute_roi), the window
aggregates, and the API endpoints. No network — deterministic fakes only."""

import os
import sys
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent.rate_limit as rate_limit_module
from agent.config import settings
from agent.models import (
    ResponseSuggestion,
    Sentiment,
    SupportTicket,
    TicketCategory,
    TicketPriority,
)
from agent.roi import DEFAULT_SETTINGS, compute_roi

DEFAULTS = DEFAULT_SETTINGS


def _raw(**overrides):
    base = {
        "total": 0,
        "auto_sent": 0,
        "responded": 0,
        "edited": 0,
        "by_day": {},
        "by_channel": {},
        "category_edits": {},
        "category_tickets": {},
        "llm_cost_usd": 0.0,
        "llm_cost_by_day": {},
    }
    base.update(overrides)
    return base


# ── Pure math ─────────────────────────────────────────────────


def test_compute_roi_full_credit_for_auto_and_partial_for_drafts():
    raw = _raw(total=10, auto_sent=4, responded=8, edited=2, llm_cost_usd=1.5)
    report = compute_roi(raw, DEFAULTS)
    # 4 auto * 10 min + 4 drafts (8 responded - 4 auto) * 5 min = 60 min = 1.0 h
    assert report["hours_saved"] == 1.0
    assert report["labor_saved_usd"] == 25.0
    assert report["net_savings_usd"] == 23.5
    assert report["roi_percent"] == 1566.7
    assert report["window"]["drafts_reviewed"] == 4
    assert report["draft_edit_rate"] == 0.25  # 2 edits / 8 responded drafts
    assert report["cost_per_ticket_usd"] == 0.15


def test_compute_roi_negative_when_llm_cost_exceeds_savings():
    raw = _raw(total=5, auto_sent=1, responded=1, llm_cost_usd=100.0)
    report = compute_roi(raw, DEFAULTS)
    saved = 10 / 60 * 25
    assert report["net_savings_usd"] == round(saved - 100.0, 2)
    assert report["roi_percent"] < 0


def test_compute_roi_empty_store_has_no_roi_claim():
    report = compute_roi(_raw(), DEFAULTS)
    assert report["hours_saved"] == 0
    assert report["net_savings_usd"] == 0
    assert report["roi_percent"] is None
    assert report["cost_per_ticket_usd"] is None
    assert report["draft_edit_rate"] == 0.0
    assert report["series"] == []


def test_compute_roi_clamps_responded_below_auto_sent():
    # A window edge shouldn't produce negative draft credit.
    raw = _raw(total=3, auto_sent=3, responded=2, llm_cost_usd=0.1)
    report = compute_roi(raw, DEFAULTS)
    assert report["window"]["drafts_reviewed"] == 0
    assert report["hours_saved"] == 0.5  # 3 * 10 min


def test_compute_roi_series_matches_per_day_math():
    raw = _raw(
        total=3,
        auto_sent=2,
        responded=3,
        llm_cost_usd=0.06,
        by_day={
            "2026-10-01": {"tickets": 2, "auto_sent": 2, "responded": 2},
            "2026-10-02": {"tickets": 1, "auto_sent": 0, "responded": 1},
        },
        llm_cost_by_day={"2026-10-01": 0.04, "2026-10-02": 0.02},
    )
    report = compute_roi(raw, DEFAULTS)
    day1, day2 = report["series"]
    assert day1["date"] == "2026-10-01"
    assert day1["hours_saved"] == round(2 * 10 / 60, 3)
    assert day1["labor_saved_usd"] == round(2 * 10 / 60 * 25, 2)
    assert day1["llm_cost_usd"] == 0.04
    assert day2["hours_saved"] == round(5 / 60, 3)  # 1 draft * 5 min
    assert day2["net_usd"] == round(5 / 60 * 25 - 0.02, 4)


def test_compute_roi_respects_custom_assumptions():
    raw = _raw(total=2, auto_sent=2, responded=2, llm_cost_usd=0.5)
    report = compute_roi(
        raw, {"minutes_per_auto_sent": 20.0, "minutes_per_draft": 10.0, "hourly_rate_usd": 50.0}
    )
    assert report["hours_saved"] == round(2 * 20 / 60, 3)
    assert report["labor_saved_usd"] == round(2 * 20 / 60 * 50, 2)


# ── API endpoints ─────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TENANT_NAME", "test")
    settings.DB_PATH = str(tmp_path / "test_roi.db")
    from agent.storage import store as _store

    _store.db_path = settings.DB_PATH
    settings.REQUIRE_API_KEY = True
    settings.API_KEY = SecretStr("test-key-123")
    settings.RATE_LIMIT_PER_MINUTE = 100
    rate_limit_module._request_log.clear()

    import importlib

    import api.customer_support as cs_module
    import api.main as main_module

    importlib.reload(cs_module)
    importlib.reload(main_module)

    with TestClient(main_module.app, raise_server_exceptions=False) as c:
        yield c, cs_module


AUTH = {"X-API-Key": "test-key-123"}


def _ticket(tid: str, created_at: str | None = None, channel="email") -> SupportTicket:
    kwargs = {"created_at": created_at} if created_at else {}
    return SupportTicket(
        id=tid,
        customer_email=f"{tid}@example.com",
        subject="t",
        body="ticket body",
        status="open",
        category=TicketCategory.ORDER_STATUS,
        priority=TicketPriority.NORMAL,
        sentiment=Sentiment.NEUTRAL,
        channel=channel,
        **kwargs,
    )


async def _seed(cs_module):
    store = cs_module.store
    await store.save(_ticket("roi-1"), auto_sent=True)
    await store.save(
        _ticket("roi-2"),
        ResponseSuggestion(
            ticket_id="roi-2",
            suggested_response="ok",
            confidence=0.9,
            reasoning="test",
            requires_human_review=True,
        ),
        auto_sent=False,
    )
    await store.add_message("roi-2", "agent", "human-approved draft")
    await store.save(
        _ticket("roi-3", created_at=(datetime.now(UTC) - timedelta(days=40)).isoformat())
    )
    await store.record_cost("roi-1", "classify", "test-model", 100, 50, 0.05)
    return store


def test_roi_endpoint_requires_api_key(client):
    c, _ = client
    assert c.get("/support/analytics/roi").status_code == 401


def test_roi_endpoint_reports_empty_store_with_default_assumptions(client):
    c, _ = client
    r = c.get("/support/analytics/roi?days=7", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["window"]["total"] == 0
    assert body["assumptions"] == DEFAULT_SETTINGS
    assert body["roi_percent"] is None
    assert body["days"] == "7"


async def test_roi_endpoint_computes_from_seeded_store(client):
    c, cs_module = client
    await _seed(cs_module)
    r = c.get("/support/analytics/roi?days=30", headers=AUTH)
    body = r.json()
    assert body["window"]["total"] == 2  # the 40-day-old ticket is outside the window
    assert body["window"]["auto_sent"] == 1
    assert body["window"]["responded"] == 1
    assert body["window"]["drafts_reviewed"] == 0
    assert body["window"]["llm_cost_usd"] == 0.05
    # 1 auto * 10 min = 1/6 h * $25
    assert body["hours_saved"] == round(10 / 60, 3)
    assert body["net_savings_usd"] == round(10 / 60 * 25 - 0.05, 2)
    assert body["by_channel"]["email"] == 2


async def test_roi_endpoint_all_window_includes_old_tickets(client):
    c, cs_module = client
    await _seed(cs_module)
    r = c.get("/support/analytics/roi?days=all", headers=AUTH)
    assert r.json()["window"]["total"] == 3
    assert r.json()["days"] == "all"


def test_roi_endpoint_rejects_bad_days(client):
    c, _ = client
    assert c.get("/support/analytics/roi?days=0", headers=AUTH).status_code == 422
    assert c.get("/support/analytics/roi?days=forever", headers=AUTH).status_code == 422


def test_roi_settings_roundtrip_affects_report(client):
    c, _ = client
    r = c.put(
        "/support/analytics/roi/settings",
        headers=AUTH,
        json={"minutes_per_auto_sent": 15, "minutes_per_draft": 7, "hourly_rate_usd": 60},
    )
    assert r.status_code == 200
    assert r.json()["assumptions"]["hourly_rate_usd"] == 60
    body = c.get("/support/analytics/roi?days=7", headers=AUTH).json()
    assert body["assumptions"]["minutes_per_auto_sent"] == 15


def test_roi_settings_reject_out_of_range(client):
    c, _ = client
    assert (
        c.put(
            "/support/analytics/roi/settings",
            headers=AUTH,
            json={"minutes_per_auto_sent": 0},
        ).status_code
        == 422
    )
    assert (
        c.put(
            "/support/analytics/roi/settings",
            headers=AUTH,
            json={"hourly_rate_usd": 99999},
        ).status_code
        == 422
    )
