"""Dead-letter queue + alerting tests (WP4).

Failed webhook payloads must be preserved (2xx to the caller, row in
dead_letters, alert fired) instead of lost, and the redrive endpoint must
replay them through the exact processor the webhook uses. Alerting is
throttled and can never raise."""

import asyncio
import importlib
import json
import os
import sys
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.alerting import reset_alert_throttle, send_alert
from agent.config import settings

AUTH = {"X-API-Key": "test-key-123"}
GORGIAS_HEADERS = {"x-webhook-secret": "gorgias-secret"}
INBOUND_HEADERS = {"x-webhook-secret": "inbound-secret"}

GORGIAS_PAYLOAD = {
    "id": "evt-dlq-001",
    "ticket": {
        "id": 9101,
        "customer": {"email": "buyer@example.test"},
        "messages": [{"body_text": "Where is my order?"}],
    },
}


@pytest.fixture(autouse=True)
def _alert_throttle_state():
    reset_alert_throttle()
    yield
    reset_alert_throttle()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-key-for-tests")
    monkeypatch.setenv("TENANT_NAME", "test")
    settings.GOOGLE_API_KEY = SecretStr("dummy-key-for-tests")
    settings.DB_PATH = str(tmp_path / "test_dead_letters.db")
    from agent.storage import store as _store

    _store.db_path = settings.DB_PATH
    settings.REQUIRE_API_KEY = True
    settings.API_KEY = SecretStr("test-key-123")
    settings.RATE_LIMIT_PER_MINUTE = 100
    settings.GORGIAS_WEBHOOK_SECRET = "gorgias-secret"
    settings.INBOUND_WEBHOOK_SECRET = "inbound-secret"

    import agent.rate_limit as rate_limit_module

    rate_limit_module._request_log.clear()

    import api.customer_support as cs_module
    import api.main as main_module
    from tests.conftest import FakeClassifier, FakeResponseEngine, FakeShopify

    importlib.reload(cs_module)
    importlib.reload(main_module)
    cs_module._agent.classifier = FakeClassifier()
    cs_module._agent.response_engine = FakeResponseEngine()
    cs_module._agent.shopify = FakeShopify()

    with TestClient(main_module.app, raise_server_exceptions=False) as c:
        yield c, cs_module


def test_dead_letters_endpoint_requires_api_key(client):
    c, _cs = client
    assert c.get("/support/dead-letters").status_code == 401
    assert c.post("/support/dead-letters/1/retry").status_code == 401


def test_gorgias_webhook_failure_dead_letters_and_returns_2xx(client):
    """A crash mid-processing must not 5xx (Gorgias would retry-storm us): the
    payload is preserved as a pending dead letter, an alert fires, caller gets 2xx."""
    c, cs = client
    cs._agent.handle_ticket = AsyncMock(side_effect=RuntimeError("LLM exploded"))
    cs.send_alert = AsyncMock(return_value=True)

    r = c.post(
        "/support/webhooks/gorgias/ticket-created",
        headers=GORGIAS_HEADERS,
        json=GORGIAS_PAYLOAD,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["received"] is True
    assert body["queued"] is True
    assert "LLM exploded" in body["error"]

    # Alert fired with the documented dedupe key.
    assert cs.send_alert.await_count == 1
    assert cs.send_alert.await_args.kwargs["dedupe_key"] == "dead_letter:gorgias:ticket-created"
    assert cs.send_alert.await_args.kwargs["severity"] == "error"

    # The operator sees the row, with the original payload intact for redrive.
    listing = c.get("/support/dead-letters", headers=AUTH)
    assert listing.status_code == 200
    data = listing.json()
    assert data["pending"] == 1
    row = data["dead_letters"][0]
    assert row["source"] == "gorgias:ticket-created"
    assert row["status"] == "pending"
    assert "RuntimeError" in row["error"]
    assert json.loads(row["payload"])["id"] == "evt-dlq-001"

    # The event was NOT marked processed — a Gorgias redelivery still processes.
    assert c.get("/support/dead-letters?status=tried", headers=AUTH).status_code == 422


def test_dead_letter_redrive_replays_and_marks_retried(client):
    """Fix the cause, hit retry: the payload runs through the real processor,
    the ticket lands, and the row flips to retried (second retry is a 409)."""
    c, cs = client
    original = cs._agent.handle_ticket
    cs._agent.handle_ticket = AsyncMock(side_effect=RuntimeError("transient outage"))

    r = c.post(
        "/support/webhooks/gorgias/ticket-created",
        headers=GORGIAS_HEADERS,
        json=GORGIAS_PAYLOAD,
    )
    assert r.status_code == 200 and r.json()["queued"] is True
    dl_id = c.get("/support/dead-letters", headers=AUTH).json()["dead_letters"][0]["id"]

    # "Outage over" — restore the real (fake-backed) pipeline.
    cs._agent.handle_ticket = original

    retry = c.post(f"/support/dead-letters/{dl_id}/retry", headers=AUTH)
    assert retry.status_code == 200, retry.text
    body = retry.json()
    assert body["status"] == "retried"
    assert body["result"]["received"] is True
    ticket_id = body["result"]["ticket_id"]
    assert c.get(f"/support/tickets/{ticket_id}", headers=AUTH).status_code == 200

    rows = c.get("/support/dead-letters?status=retried", headers=AUTH).json()["dead_letters"]
    assert len(rows) == 1
    assert rows[0]["attempts"] == 1

    # Redriving a successful replay is a conflict, not a double-process.
    again = c.post(f"/support/dead-letters/{dl_id}/retry", headers=AUTH)
    assert again.status_code == 409
    assert again.json()["error"] == "ALREADY_RETRIED"


def test_dead_letter_redrive_failure_marks_failed(client):
    """A redrive that fails again flips the row to failed (attempts bumped) and
    returns 502 — the payload stays listed for the next attempt."""
    c, cs = client
    cs._agent.handle_ticket = AsyncMock(side_effect=RuntimeError("still broken"))

    r = c.post(
        "/support/webhooks/gorgias/ticket-created",
        headers=GORGIAS_HEADERS,
        json=GORGIAS_PAYLOAD,
    )
    assert r.status_code == 200
    dl_id = c.get("/support/dead-letters", headers=AUTH).json()["dead_letters"][0]["id"]

    retry = c.post(f"/support/dead-letters/{dl_id}/retry", headers=AUTH)
    assert retry.status_code == 502
    assert retry.json()["error"] == "REDRIVE_FAILED"

    rows = c.get("/support/dead-letters?status=failed", headers=AUTH).json()["dead_letters"]
    assert len(rows) == 1
    assert rows[0]["attempts"] == 1
    assert "still broken" in rows[0]["last_error"]


def test_inbound_webhook_failure_dead_letters_and_redrives(client):
    c, cs = client
    original = cs._agent.handle_ticket
    cs._agent.handle_ticket = AsyncMock(side_effect=RuntimeError("inbound boom"))

    r = c.post(
        "/support/webhooks/inbound",
        headers=INBOUND_HEADERS,
        json={"channel": "chat", "customer_email": "a@b.com", "body": "hi there"},
    )
    assert r.status_code == 200 and r.json()["queued"] is True
    dl_id = c.get("/support/dead-letters", headers=AUTH).json()["dead_letters"][0]["id"]
    assert (
        c.get("/support/dead-letters", headers=AUTH).json()["dead_letters"][0]["source"]
        == "inbound"
    )

    cs._agent.handle_ticket = original
    retry = c.post(f"/support/dead-letters/{dl_id}/retry", headers=AUTH)
    assert retry.status_code == 200, retry.text
    assert retry.json()["result"]["ticket_id"].startswith("inbound_")


def test_redrive_unknown_source_returns_422(client):
    c, _cs = client
    from agent.storage import store

    dl_id = asyncio.run(
        store.record_dead_letter(source="carrier:tracking-update", payload={"x": 1}, error="boom")
    )
    r = c.post(f"/support/dead-letters/{dl_id}/retry", headers=AUTH)
    assert r.status_code == 422
    assert r.json()["error"] == "UNSUPPORTED_SOURCE"


def test_redrive_missing_row_returns_404(client):
    c, _cs = client
    r = c.post("/support/dead-letters/999999/retry", headers=AUTH)
    assert r.status_code == 404


def test_respond_failure_alerts_and_returns_502(client):
    """Human-approved send that dies at Gorgias: 502 REPLY_FAILED + alert, and the
    gorgias circuit stays closed (an unexpected error is not an upstream-health signal)."""
    c, cs = client
    created = c.post(
        "/support/tickets",
        headers=AUTH,
        json={
            "customer_email": "buyer@example.test",
            "subject": "Order question",
            "body": "Where is my order 1042?",
            "metadata": {"gorgias_ticket_id": "g888"},
        },
    )
    assert created.status_code == 200, created.text
    ticket_id = created.json()["ticket_id"]

    cs._gorgias.post_reply = AsyncMock(side_effect=RuntimeError("Gorgias 500s"))
    cs.send_alert = AsyncMock(return_value=True)

    resp = c.post(
        f"/support/tickets/{ticket_id}/respond",
        headers=AUTH,
        json={"response": "Checking now!", "send_via_gorgias": True},
    )
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"] == "REPLY_FAILED"

    assert cs.send_alert.await_count == 1
    assert cs.send_alert.await_args.kwargs["dedupe_key"] == "respond_failed"

    from integrations.gorgias import GORGIAS_BREAKER

    assert GORGIAS_BREAKER.state == "closed"


def test_cost_cap_breach_sends_alert(client, monkeypatch):
    _c, _cs = client
    from agent.observability import check_daily_cost_cap
    from agent.storage import store

    spy = AsyncMock(return_value=True)
    monkeypatch.setattr("agent.alerting.send_alert", spy)
    monkeypatch.setattr(settings, "DAILY_COST_CAP_USD", 1.0)
    monkeypatch.setattr(store, "get_today_cost_usd", AsyncMock(return_value=42.5))

    within = asyncio.run(check_daily_cost_cap())
    assert within is False
    assert spy.await_count == 1
    assert spy.await_args.kwargs["dedupe_key"] == "daily_cost_cap"
    assert spy.await_args.kwargs["severity"] == "critical"


# ── agent.alerting unit behaviour ─────────────────────────────


class _FakeResponse:
    def raise_for_status(self):
        return None


def _fake_httpx_client(posts):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, **kwargs):
            posts.append((url, json))
            return _FakeResponse()

    return FakeClient


def _broken_httpx_client(posts):
    class BrokenClient(_fake_httpx_client(posts)):
        async def post(self, url, json=None, **kwargs):
            posts.append((url, json))
            raise httpx.ConnectError("webhook endpoint unreachable")

    return BrokenClient


@pytest.mark.asyncio
async def test_send_alert_posts_and_throttles(monkeypatch):
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_URL", "https://hooks.example.test/x")
    posts: list = []
    monkeypatch.setattr("agent.alerting.httpx.AsyncClient", _fake_httpx_client(posts))

    assert await send_alert("Circuit opened: gorgias", "body", dedupe_key="c:gorgias") is True
    assert len(posts) == 1
    url, body = posts[0]
    assert url == "https://hooks.example.test/x"
    assert body["title"] == "Circuit opened: gorgias"
    assert body["text"].startswith("[WARNING] Circuit opened: gorgias")

    # Second alert with the same dedupe key inside the window is suppressed.
    assert await send_alert("Circuit opened: gorgias", "again", dedupe_key="c:gorgias") is False
    assert len(posts) == 1

    # A different key is independent.
    assert await send_alert("Other", "body", dedupe_key="other") is True
    assert len(posts) == 2


@pytest.mark.asyncio
async def test_send_alert_swallows_http_errors(monkeypatch):
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_URL", "https://hooks.example.test/x")
    posts: list = []
    monkeypatch.setattr("agent.alerting.httpx.AsyncClient", _broken_httpx_client(posts))

    assert await send_alert("t", "m", dedupe_key="broken") is False


def test_send_alert_unconfigured_is_noop(monkeypatch):
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_URL", None)
    assert asyncio.run(send_alert("t", "m")) is False
