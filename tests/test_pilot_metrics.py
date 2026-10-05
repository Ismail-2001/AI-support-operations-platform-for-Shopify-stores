"""Pilot metrics tests (WP6).

GET /support/analytics/pilot must report auto-resolve %, edit rate, TTFR and
LLM spend from real rows — windowed correctly, with TTFR measured only for
tickets that actually got a reply."""

import importlib
import os
import sys
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.models import SupportTicket

T0 = "2026-01-01T00:00:00+00:00"


async def _insert_message(store, ticket_id, sender, content, created_at):
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO messages (ticket_id, sender_type, content, created_at)"
            " VALUES (?, ?, ?, ?)",
            (ticket_id, sender, content, created_at),
        )
        await db.commit()


async def _insert_cost(store, date, cost_usd):
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO llm_costs (date, ticket_id, stage, model, tokens_input,"
            " tokens_output, cost_usd, created_at) VALUES (?, 't', 'classification',"
            " 'model', 10, 5, ?, ?)",
            (date, cost_usd, f"{date}T00:00:00+00:00"),
        )
        await db.commit()


async def _seed_headline_rows(store):
    """2/4 auto-resolved, 1/2 drafts edited, TTFR 10s and 30s, $0.03 spend."""
    for i, auto in enumerate([True, True, False, False]):
        await store.save(
            SupportTicket(id=f"p{i}", customer_email="a@b.com", subject="s", body="b"),
            auto_sent=auto,
        )
    # One unchanged draft, one rewritten draft.
    await store.log_edit("p0", "Same reply", "Same reply", category="shipping", confidence=0.9)
    await store.log_edit(
        "p1", "Draft reply", "Totally rewritten", category="shipping", confidence=0.9
    )

    await _insert_message(store, "p0", "customer", "hi", T0)
    await _insert_message(store, "p0", "ai", "hello", "2026-01-01T00:00:10+00:00")
    await _insert_message(store, "p1", "customer", "hey", T0)
    await _insert_message(store, "p1", "agent", "hi there", "2026-01-01T00:00:30+00:00")
    # p2: customer wrote, nobody replied — excluded from TTFR.
    await _insert_message(store, "p2", "customer", "waiting", T0)

    await _insert_cost(store, "2026-01-01", 0.01)
    await _insert_cost(store, "2026-01-02", 0.02)


async def test_pilot_metrics_aggregate_headline_numbers(test_store):
    await _seed_headline_rows(test_store)
    m = await test_store.get_pilot_metrics(None)

    assert m["tickets"] == {"total": 4, "auto_sent": 2, "auto_resolve_rate": 0.5}
    assert m["edits"] == {
        "total_ai_drafts_sent": 2,
        "edited_before_send": 1,
        "edit_rate": 0.5,
    }
    assert m["ttfr_seconds"]["measured_tickets"] == 2
    assert m["ttfr_seconds"]["mean"] == 20.0
    assert m["ttfr_seconds"]["median"] == 20.0
    assert m["llm_cost_usd"] == pytest.approx(0.03)


async def test_pilot_metrics_empty_store_returns_safe_nulls(test_store):
    m = await test_store.get_pilot_metrics(None)
    assert m["tickets"]["total"] == 0
    assert m["tickets"]["auto_resolve_rate"] is None
    assert m["edits"]["edit_rate"] is None
    assert m["ttfr_seconds"]["measured_tickets"] == 0
    assert m["ttfr_seconds"]["mean"] is None
    assert m["llm_cost_usd"] == 0.0


async def test_pilot_metrics_window_excludes_old_rows(test_store):
    old = datetime(2020, 1, 1, tzinfo=UTC)
    await test_store.save(
        SupportTicket(
            id="ancient",
            customer_email="a@b.com",
            subject="s",
            body="b",
            created_at=old.isoformat(),
        ),
        auto_sent=True,
    )
    await test_store.save(
        SupportTicket(id="fresh", customer_email="a@b.com", subject="s", body="b"),
        auto_sent=False,
    )

    since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    m = await test_store.get_pilot_metrics(since)
    assert m["tickets"]["total"] == 1, "2020 ticket must fall outside a 1-day window"
    assert m["tickets"]["auto_resolve_rate"] == 0.0


# ── HTTP surface ─────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-key-for-tests")
    monkeypatch.setenv("TENANT_NAME", "test")
    from agent.config import settings

    settings.GOOGLE_API_KEY = SecretStr("dummy-key-for-tests")
    settings.DB_PATH = str(tmp_path / "test_pilot.db")
    from agent.storage import store as _store

    _store.db_path = settings.DB_PATH
    settings.REQUIRE_API_KEY = True
    settings.API_KEY = SecretStr("test-key-123")
    settings.RATE_LIMIT_PER_MINUTE = 100

    import agent.rate_limit as rate_limit_module

    rate_limit_module._request_log.clear()

    import api.customer_support as cs_module
    import api.main as main_module
    from tests.conftest import FakeClassifier, FakeResponseEngine, FakeShopify

    importlib.reload(cs_module)
    importlib.reload(main_module)
    # Swap the real LLM/Shopify components for fakes — POST /support/tickets
    # must never hit a real provider from tests.
    cs_module._agent.classifier = FakeClassifier()
    cs_module._agent.response_engine = FakeResponseEngine()
    cs_module._agent.shopify = FakeShopify()

    with TestClient(main_module.app, raise_server_exceptions=False) as c:
        yield c


AUTH = {"X-API-Key": "test-key-123"}


def test_pilot_endpoint_requires_api_key(client):
    assert client.get("/support/analytics/pilot").status_code == 401


def test_pilot_endpoint_rejects_bad_days(client):
    assert client.get("/support/analytics/pilot?days=soon", headers=AUTH).status_code == 422


def test_pilot_endpoint_returns_pilot_shape(client):
    r = client.get("/support/analytics/pilot?days=all", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_days"] == "all"
    assert body["since"] is None
    assert set(body["tickets"]) == {"total", "auto_sent", "auto_resolve_rate"}
    assert set(body["edits"]) == {"total_ai_drafts_sent", "edited_before_send", "edit_rate"}
    assert set(body["ttfr_seconds"]) == {"measured_tickets", "mean", "median"}
    assert isinstance(body["llm_cost_usd"], float)


def test_pilot_endpoint_numbers_come_from_store(client):
    c = client
    # Seed one auto-resolved ticket through the real API path.
    r = c.post(
        "/support/tickets",
        headers=AUTH,
        json={"customer_email": "a@b.com", "subject": "s", "body": "b"},
    )
    assert r.status_code == 200

    body = c.get("/support/analytics/pilot?days=all", headers=AUTH).json()
    assert body["tickets"]["total"] == 1
