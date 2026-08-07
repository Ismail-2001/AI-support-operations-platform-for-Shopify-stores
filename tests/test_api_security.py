"""
API-level security tests. Uses FastAPI's TestClient (in-process, no real network) against
the actual app + real SQLite (temp file). The agent's classifier/response_engine/shopify
are swapped for fakes after import — these tests exercise OUR auth/rate-limit/validation
wiring, not Google's or Shopify's APIs, so they run in CI with no API keys needed.
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent.rate_limit as rate_limit_module
from agent.config import settings
from tests.conftest import FakeClassifier, FakeResponseEngine, FakeShopify


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-key-for-tests")
    monkeypatch.setenv("TENANT_NAME", "test")  # already set by conftest, explicit for safety
    settings.GOOGLE_API_KEY = SecretStr("dummy-key-for-tests")
    settings.DB_PATH = str(tmp_path / "test_api.db")
    # Update the module-level store singleton's db_path — it caches at import time
    from agent.storage import store as _store

    _store.db_path = settings.DB_PATH
    settings.REQUIRE_API_KEY = True
    settings.API_KEY = SecretStr("test-key-123")
    settings.RATE_LIMIT_PER_MINUTE = 100
    settings.REFUND_RATE_LIMIT_PER_MINUTE = 100
    settings.GORGIAS_WEBHOOK_SECRET = "gorgias-secret"
    settings.INBOUND_WEBHOOK_SECRET = "inbound-secret"
    rate_limit_module._request_log.clear()

    import importlib

    import api.customer_support as cs_module
    import api.main as main_module

    importlib.reload(cs_module)
    importlib.reload(main_module)

    # Swap the real LLM/Shopify components for deterministic fakes — no network calls,
    # no API key needed, tests run identically in CI as they do locally.
    main_module.support_router
    cs_module._agent.classifier = FakeClassifier()
    cs_module._agent.response_engine = FakeResponseEngine()
    cs_module._agent.shopify = FakeShopify()

    with TestClient(main_module.app, raise_server_exceptions=False) as c:
        yield c, cs_module


AUTH = {"X-API-Key": "test-key-123"}


def test_protected_endpoint_rejects_missing_api_key(client):
    c, _ = client
    r = c.get("/support/tickets")
    assert r.status_code == 401


def test_protected_endpoint_rejects_wrong_api_key(client):
    c, _ = client
    r = c.get("/support/tickets", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_protected_endpoint_accepts_correct_api_key(client):
    c, _ = client
    r = c.get("/support/tickets", headers=AUTH)
    assert r.status_code == 200


def test_health_endpoints_require_no_api_key(client):
    c, _ = client
    assert c.get("/health").status_code == 200
    assert c.get("/support/health").status_code == 200


def test_gorgias_webhook_rejects_missing_secret(client):
    c, _ = client
    r = c.post("/support/webhooks/gorgias/ticket-created", json={"ticket": {"id": 1}})
    assert r.status_code == 401


def test_gorgias_webhook_rejects_wrong_secret(client):
    c, _ = client
    r = c.post(
        "/support/webhooks/gorgias/ticket-created",
        headers={"x-webhook-secret": "wrong"},
        json={"ticket": {"id": 1}},
    )
    assert r.status_code == 401


def test_inbound_webhook_rejects_missing_secret(client):
    c, _ = client
    r = c.post(
        "/support/webhooks/inbound",
        json={"channel": "chat", "customer_email": "a@b.com", "body": "hi"},
    )
    assert r.status_code == 401


def test_inbound_webhook_accepts_correct_secret(client):
    c, _ = client
    r = c.post(
        "/support/webhooks/inbound",
        headers={"x-webhook-secret": "inbound-secret"},
        json={"channel": "chat", "customer_email": "a@b.com", "body": "hi"},
    )
    assert r.status_code == 200


def test_refund_requires_idempotency_key_header(client):
    c, _ = client
    r = c.post("/support/tickets/nonexistent/actions/refund", headers=AUTH, json={"amount": 10})
    assert r.status_code == 422


def test_refund_on_nonexistent_ticket_returns_404(client):
    c, _ = client
    r = c.post(
        "/support/tickets/nonexistent/actions/refund",
        headers={**AUTH, "Idempotency-Key": "test-key-1"},
        json={"amount": 10},
    )
    assert r.status_code == 404


def test_refund_without_linked_order_returns_400(client):
    c, _ = client
    r = c.post(
        "/support/tickets",
        headers=AUTH,
        json={
            "customer_email": "a@b.com",
            "subject": "test",
            "body": "test",
        },
    )
    assert r.status_code == 200
    ticket_id = r.json()["ticket_id"]

    r2 = c.post(
        f"/support/tickets/{ticket_id}/actions/refund",
        headers={**AUTH, "Idempotency-Key": "test-key-2"},
        json={"amount": 10},
    )
    assert r2.status_code == 400
    assert "order_id" in r2.json()["message"]


def test_refund_idempotency_replays_instead_of_double_refunding(client):
    """The highest-stakes test in this suite: sending the same Idempotency-Key twice
    must not process the refund twice."""
    import asyncio

    c, cs_module = client

    class FakeShopifyWithRefund:
        enabled = True
        call_count = 0

        async def get_order_by_id(self, order_id):
            return {"id": order_id, "total_price": "100.00"}

        async def create_refund(self, order_id, amount, reason="", notify_customer=True):
            self.call_count += 1
            return {"refund": {"id": 777, "call_number": self.call_count}}

    fake_shopify = FakeShopifyWithRefund()
    cs_module._agent.shopify = fake_shopify

    r = c.post(
        "/support/tickets",
        headers=AUTH,
        json={
            "customer_email": "a@b.com",
            "subject": "test",
            "body": "test",
        },
    )
    ticket_id = r.json()["ticket_id"]

    # Manually attach an order_id the way a real order-related ticket would get one
    asyncio.run(
        __import__("agent.storage", fromlist=["store"]).store.update_status(
            ticket_id, order_id="999"
        )
    )

    headers = {**AUTH, "Idempotency-Key": "same-key-used-twice"}
    r1 = c.post(
        f"/support/tickets/{ticket_id}/actions/refund", headers=headers, json={"amount": 20.0}
    )
    r2 = c.post(
        f"/support/tickets/{ticket_id}/actions/refund", headers=headers, json={"amount": 20.0}
    )

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["replayed"] is False
    assert r2.json()["replayed"] is True
    assert (
        fake_shopify.call_count == 1
    ), "create_refund must only be called ONCE across both requests"


def test_refund_rejects_amount_exceeding_order_total(client):
    import asyncio

    c, cs_module = client

    class FakeShopifyCapped:
        enabled = True

        async def get_order_by_id(self, order_id):
            return {"id": order_id, "total_price": "50.00"}

        async def create_refund(self, **kwargs):
            raise AssertionError(
                "create_refund should never be called when amount exceeds order total"
            )

    cs_module._agent.shopify = FakeShopifyCapped()

    r = c.post(
        "/support/tickets",
        headers=AUTH,
        json={
            "customer_email": "a@b.com",
            "subject": "test",
            "body": "test",
        },
    )
    ticket_id = r.json()["ticket_id"]
    asyncio.run(
        __import__("agent.storage", fromlist=["store"]).store.update_status(
            ticket_id, order_id="999"
        )
    )

    r2 = c.post(
        f"/support/tickets/{ticket_id}/actions/refund",
        headers={**AUTH, "Idempotency-Key": "cap-test-key"},
        json={"amount": 999.0},
    )
    assert r2.status_code == 400
    assert "exceeds" in r2.json()["message"]


# ── Resend order action ────────────────────────────────────────


def test_resend_order_requires_idempotency_key_header(client):
    c, _ = client
    r = c.post("/support/tickets/nonexistent/actions/resend-order", headers=AUTH, json={})
    assert r.status_code == 422


def test_resend_order_on_nonexistent_ticket_returns_404(client):
    c, _ = client
    r = c.post(
        "/support/tickets/nonexistent/actions/resend-order",
        headers={**AUTH, "Idempotency-Key": "resend-key-1"},
        json={},
    )
    assert r.status_code == 404


def test_resend_order_without_linked_order_returns_400(client):
    c, _ = client
    r = c.post(
        "/support/tickets",
        headers=AUTH,
        json={
            "customer_email": "a@b.com",
            "subject": "test",
            "body": "test",
        },
    )
    assert r.status_code == 200
    ticket_id = r.json()["ticket_id"]

    r2 = c.post(
        f"/support/tickets/{ticket_id}/actions/resend-order",
        headers={**AUTH, "Idempotency-Key": "resend-key-2"},
        json={},
    )
    assert r2.status_code == 400
    assert "order_id" in r2.json()["message"]


def test_resend_order_idempotency_replays_instead_of_double_reordering(client):
    """Same Idempotency-Key twice -> second call returns replayed=True, create_reorder runs once."""
    import asyncio

    c, cs_module = client

    class FakeShopifyWithResend:
        enabled = True
        call_count = 0

        async def get_order_by_id(self, order_id):
            return {"id": order_id, "total_price": "100.00"}

        async def create_reorder(self, order_id, notify_customer=True):
            self.call_count += 1
            return {
                "new_order_id": 888,
                "original_order_id": order_id,
                "call_number": self.call_count,
            }

    fake_shopify = FakeShopifyWithResend()
    cs_module._agent.shopify = fake_shopify

    r = c.post(
        "/support/tickets",
        headers=AUTH,
        json={
            "customer_email": "a@b.com",
            "subject": "test",
            "body": "test",
        },
    )
    ticket_id = r.json()["ticket_id"]

    asyncio.run(
        __import__("agent.storage", fromlist=["store"]).store.update_status(
            ticket_id, order_id="999"
        )
    )

    headers = {**AUTH, "Idempotency-Key": "resend-same-key"}
    r1 = c.post(f"/support/tickets/{ticket_id}/actions/resend-order", headers=headers, json={})
    r2 = c.post(f"/support/tickets/{ticket_id}/actions/resend-order", headers=headers, json={})

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["replayed"] is False
    assert r2.json()["replayed"] is True
    assert (
        fake_shopify.call_count == 1
    ), "create_reorder must only be called ONCE across both requests"


def test_rate_limit_returns_429_when_exceeded(client):
    c, _ = client
    settings.RATE_LIMIT_PER_MINUTE = 3
    rate_limit_module._request_log.clear()
    statuses = [c.get("/support/tickets", headers=AUTH).status_code for _ in range(6)]
    assert 429 in statuses


def test_unhandled_exception_does_not_leak_internals(client):
    c, cs_module = client

    class BrokenClassifier:
        async def classify(self, ticket, history=None):
            raise ValueError("some internal detail that should never reach the client")

    cs_module._agent.classifier = BrokenClassifier()

    r = c.post(
        "/support/tickets",
        headers=AUTH,
        json={
            "customer_email": "a@b.com",
            "subject": "test",
            "body": "test",
        },
    )
    assert r.status_code == 500
    assert r.json() == {"error": "INTERNAL_SERVER_ERROR", "message": "Internal server error"}
    assert "some internal detail" not in r.text


# ── Gorgias webhook idempotency ─────────────────────────────


def test_gorgias_webhook_idempotency_duplicate_event_id(client):
    """Same event_id sent twice -> second call returns duplicate:true, pipeline runs once."""
    c, cs_module = client

    call_count = [0]
    original = cs_module._agent.handle_ticket

    async def counting_handle_ticket(ticket):
        call_count[0] += 1
        return await original(ticket)

    cs_module._agent.handle_ticket = counting_handle_ticket

    payload = {
        "id": "evt-dup-001",
        "ticket": {
            "id": 5001,
            "customer": {"email": "a@b.com"},
            "messages": [{"body_text": "Help"}],
        },
    }
    headers = {"x-webhook-secret": "gorgias-secret"}

    r1 = c.post("/support/webhooks/gorgias/ticket-created", headers=headers, json=payload)
    assert r1.status_code == 200
    assert r1.json()["received"] is True
    assert r1.json().get("duplicate") is not True

    r2 = c.post("/support/webhooks/gorgias/ticket-created", headers=headers, json=payload)
    assert r2.status_code == 200
    assert r2.json()["received"] is True
    assert r2.json()["duplicate"] is True

    assert call_count[0] == 1, "handle_ticket must run exactly once across both deliveries"


def test_gorgias_webhook_idempotency_different_event_ids(client):
    """Different event_ids -> both process normally."""
    c, cs_module = client

    call_count = [0]
    original = cs_module._agent.handle_ticket

    async def counting_handle_ticket(ticket):
        call_count[0] += 1
        return await original(ticket)

    cs_module._agent.handle_ticket = counting_handle_ticket

    headers = {"x-webhook-secret": "gorgias-secret"}

    r1 = c.post(
        "/support/webhooks/gorgias/ticket-created",
        headers=headers,
        json={
            "id": "evt-diff-001",
            "ticket": {
                "id": 5002,
                "customer": {"email": "a@b.com"},
                "messages": [{"body_text": "First"}],
            },
        },
    )
    r2 = c.post(
        "/support/webhooks/gorgias/ticket-created",
        headers=headers,
        json={
            "id": "evt-diff-002",
            "ticket": {
                "id": 5003,
                "customer": {"email": "a@b.com"},
                "messages": [{"body_text": "Second"}],
            },
        },
    )

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json().get("duplicate") is not True
    assert r2.json().get("duplicate") is not True
    assert call_count[0] == 2


def test_gorgias_webhook_idempotency_missing_event_id(client):
    """Missing event_id in payload -> doesn't crash, processes normally (logs a warning)."""
    c, cs_module = client

    call_count = [0]
    original = cs_module._agent.handle_ticket

    async def counting_handle_ticket(ticket):
        call_count[0] += 1
        return await original(ticket)

    cs_module._agent.handle_ticket = counting_handle_ticket

    headers = {"x-webhook-secret": "gorgias-secret"}
    payload = {
        "ticket": {
            "id": 5004,
            "customer": {"email": "a@b.com"},
            "messages": [{"body_text": "No event id"}],
        },
    }

    r = c.post("/support/webhooks/gorgias/ticket-created", headers=headers, json=payload)
    assert r.status_code == 200
    assert r.json()["received"] is True
    assert r.json().get("duplicate") is not True
    assert call_count[0] == 1
