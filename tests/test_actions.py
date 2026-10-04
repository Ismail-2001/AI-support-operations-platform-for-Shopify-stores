"""Human-approved action endpoints: cancel order, edit shipping address, partial refund.

Same harness as test_api_security.py — FastAPI TestClient + temp SQLite + fake Shopify.
These tests prove the invariants the product promises: nothing executes without a human
explicit call, every call carries an Idempotency-Key, replayed keys never re-execute, and
guard rails (already cancelled, already shipped, over-refund) refuse the request instead
of quietly doing the wrong thing.
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
    monkeypatch.setenv("TENANT_NAME", "test")
    settings.GOOGLE_API_KEY = SecretStr("dummy-key-for-tests")
    settings.DB_PATH = str(tmp_path / "test_actions.db")
    from agent.storage import store as _store

    _store.db_path = settings.DB_PATH
    settings.REQUIRE_API_KEY = True
    settings.API_KEY = SecretStr("test-key-123")
    settings.RATE_LIMIT_PER_MINUTE = 100
    settings.REFUND_RATE_LIMIT_PER_MINUTE = 100
    settings.ACTION_RATE_LIMIT_PER_MINUTE = 100
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
        yield c, cs_module


AUTH = {"X-API-Key": "test-key-123"}


def _create_ticket(c, body="please help", subject="help"):
    r = c.post(
        "/support/tickets",
        headers=AUTH,
        json={"customer_email": "a@b.com", "subject": subject, "body": body},
    )
    assert r.status_code == 200, r.text
    return r.json()["ticket_id"]


def _link_order(ticket_id, order_id="999"):
    import asyncio

    import agent.storage as storage_module

    asyncio.run(storage_module.store.update_status(ticket_id, order_id=order_id))


# ── Cancel order ────────────────────────────────────────────


class FakeShopifyCancellable:
    enabled = True
    cancel_calls = 0
    cancelled_at = None
    fulfillment_status = None
    fail_with = None

    def __init__(self):
        self.cancel_calls = 0

    async def get_order_by_id(self, order_id):
        return {
            "id": order_id,
            "total_price": "50.00",
            "cancelled_at": self.cancelled_at,
            "fulfillment_status": self.fulfillment_status,
        }

    async def cancel_order(self, order_id, reason="", notify_customer=True):
        self.cancel_calls += 1
        if self.fail_with:
            raise self.fail_with
        return {"order": {"id": order_id, "cancelled": True, "reason": reason}}


def test_cancel_requires_idempotency_key(client):
    c, _ = client
    r = c.post("/support/tickets/nonexistent/actions/cancel", headers=AUTH, json={})
    assert r.status_code == 422


def test_cancel_on_nonexistent_ticket_returns_404(client):
    c, _ = client
    r = c.post(
        "/support/tickets/nonexistent/actions/cancel",
        headers={**AUTH, "Idempotency-Key": "k1"},
        json={},
    )
    assert r.status_code == 404


def test_cancel_without_linked_order_returns_400(client):
    c, _ = client
    ticket_id = _create_ticket(c)
    r = c.post(
        f"/support/tickets/{ticket_id}/actions/cancel",
        headers={**AUTH, "Idempotency-Key": "k2"},
        json={},
    )
    assert r.status_code == 400
    assert "order_id" in r.json()["message"]


def test_cancel_idempotency_replays_instead_of_double_cancelling(client):
    c, cs_module = client
    fake = FakeShopifyCancellable()
    cs_module._agent.shopify = fake

    ticket_id = _create_ticket(c)
    _link_order(ticket_id)

    headers = {**AUTH, "Idempotency-Key": "cancel-same-key"}
    r1 = c.post(f"/support/tickets/{ticket_id}/actions/cancel", headers=headers, json={})
    r2 = c.post(f"/support/tickets/{ticket_id}/actions/cancel", headers=headers, json={})

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["replayed"] is False
    assert r2.json()["replayed"] is True
    assert fake.cancel_calls == 1, "cancel_order must only be called ONCE across both requests"


def test_cancel_already_cancelled_order_returns_409(client):
    c, cs_module = client
    fake = FakeShopifyCancellable()
    fake.cancelled_at = "2026-01-01T00:00:00Z"
    cs_module._agent.shopify = fake

    ticket_id = _create_ticket(c)
    _link_order(ticket_id)

    r = c.post(
        f"/support/tickets/{ticket_id}/actions/cancel",
        headers={**AUTH, "Idempotency-Key": "k3"},
        json={},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "ORDER_ALREADY_CANCELLED"
    assert fake.cancel_calls == 0


def test_cancel_fulfilled_order_returns_409(client):
    c, cs_module = client
    fake = FakeShopifyCancellable()
    fake.fulfillment_status = "fulfilled"
    cs_module._agent.shopify = fake

    ticket_id = _create_ticket(c)
    _link_order(ticket_id)

    r = c.post(
        f"/support/tickets/{ticket_id}/actions/cancel",
        headers={**AUTH, "Idempotency-Key": "k4"},
        json={},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "ORDER_ALREADY_FULFILLED"
    assert fake.cancel_calls == 0


def test_cancel_success_audits_and_resolves_ticket(client):
    c, cs_module = client
    fake = FakeShopifyCancellable()
    cs_module._agent.shopify = fake

    ticket_id = _create_ticket(c)
    _link_order(ticket_id)

    r = c.post(
        f"/support/tickets/{ticket_id}/actions/cancel",
        headers={**AUTH, "Idempotency-Key": "k5"},
        json={"reason": "customer changed their mind", "notify_customer": True},
    )
    assert r.status_code == 200
    assert r.json()["cancel"]["order"]["cancelled"] is True

    # Audit row exists with who/what/why
    import asyncio

    import agent.storage as storage_module

    audit = asyncio.run(storage_module.store.get_action_audit("k5"))
    assert audit is not None
    assert audit["action"] == "cancel_order"
    assert audit["status"] == "succeeded"
    assert audit["request"]["reason"] == "customer changed their mind"

    # Ticket resolved + action note left in the thread
    messages = asyncio.run(storage_module.store.get_messages(ticket_id))
    assert any("[Action taken] Order cancelled" in m.content for m in messages)


def test_cancel_shopify_rejection_becomes_409_with_failed_audit(client):
    c, cs_module = client
    fake = FakeShopifyCancellable()
    fake.fail_with = ValueError("Order 999 cannot be cancelled: already cancelled")
    cs_module._agent.shopify = fake

    ticket_id = _create_ticket(c)
    _link_order(ticket_id)

    r = c.post(
        f"/support/tickets/{ticket_id}/actions/cancel",
        headers={**AUTH, "Idempotency-Key": "k6"},
        json={},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "ORDER_CANNOT_CANCEL"

    import asyncio

    import agent.storage as storage_module

    audit = asyncio.run(storage_module.store.get_action_audit("k6"))
    assert audit["status"] == "failed"


def test_cancel_idempotency_key_cannot_be_reused_for_other_action(client):
    c, cs_module = client
    fake = FakeShopifyCancellable()
    cs_module._agent.shopify = fake

    ticket_id = _create_ticket(c)
    _link_order(ticket_id)

    headers = {**AUTH, "Idempotency-Key": "shared-key"}
    r1 = c.post(f"/support/tickets/{ticket_id}/actions/cancel", headers=headers, json={})
    assert r1.status_code == 200
    r2 = c.post(
        f"/support/tickets/{ticket_id}/actions/edit-address",
        headers=headers,
        json={
            "address": {
                "address1": "1 Main St",
                "city": "Springfield",
                "country": "US",
                "zip": "12345",
            }
        },
    )
    assert r2.status_code == 409
    assert r2.json()["error"] == "IDEMPOTENCY_KEY_CONFLICT"


# ── Edit shipping address ───────────────────────────────────


class FakeShopifyAddress:
    enabled = True
    update_calls = 0
    cancelled_at = None
    fulfillment_status = None
    fail_with = None

    def __init__(self):
        self.update_calls = 0
        self.received_address = None

    async def get_order_by_id(self, order_id):
        return {
            "id": order_id,
            "total_price": "30.00",
            "cancelled_at": self.cancelled_at,
            "fulfillment_status": self.fulfillment_status,
            "shipping_address": {
                "address1": "123 Old Street",
                "city": "Springfield",
                "zip": "00001",
                "country": "United States",
            },
        }

    async def update_shipping_address(self, order_id, address):
        self.update_calls += 1
        if self.fail_with:
            raise self.fail_with
        self.received_address = address
        return {"order": {"id": order_id, "shipping_address": address}}


VALID_ADDRESS = {
    "address1": "456 New Avenue",
    "city": "Shelbyville",
    "zip": "00002",
    "country": "United States",
}


def test_edit_address_requires_idempotency_key(client):
    c, _ = client
    r = c.post(
        "/support/tickets/nonexistent/actions/edit-address",
        headers=AUTH,
        json={"address": VALID_ADDRESS},
    )
    assert r.status_code == 422


def test_edit_address_rejects_missing_required_fields(client):
    c, _ = client
    ticket_id = _create_ticket(c)
    _link_order(ticket_id)
    r = c.post(
        f"/support/tickets/{ticket_id}/actions/edit-address",
        headers={**AUTH, "Idempotency-Key": "e1"},
        json={"address": {"address1": "1 Main St"}},  # no city/country/zip
    )
    assert r.status_code == 422


def test_edit_address_fulfilled_order_returns_409(client):
    c, cs_module = client
    fake = FakeShopifyAddress()
    fake.fulfillment_status = "fulfilled"
    cs_module._agent.shopify = fake

    ticket_id = _create_ticket(c)
    _link_order(ticket_id)

    r = c.post(
        f"/support/tickets/{ticket_id}/actions/edit-address",
        headers={**AUTH, "Idempotency-Key": "e2"},
        json={"address": VALID_ADDRESS},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "ORDER_ALREADY_FULFILLED"
    assert fake.update_calls == 0


def test_edit_address_cancelled_order_returns_409(client):
    c, cs_module = client
    fake = FakeShopifyAddress()
    fake.cancelled_at = "2026-01-01T00:00:00Z"
    cs_module._agent.shopify = fake

    ticket_id = _create_ticket(c)
    _link_order(ticket_id)

    r = c.post(
        f"/support/tickets/{ticket_id}/actions/edit-address",
        headers={**AUTH, "Idempotency-Key": "e3"},
        json={"address": VALID_ADDRESS},
    )
    assert r.status_code == 409
    assert fake.update_calls == 0


def test_edit_address_success_returns_both_addresses_and_audits(client):
    c, cs_module = client
    fake = FakeShopifyAddress()
    cs_module._agent.shopify = fake

    ticket_id = _create_ticket(c)
    _link_order(ticket_id)

    r = c.post(
        f"/support/tickets/{ticket_id}/actions/edit-address",
        headers={**AUTH, "Idempotency-Key": "e4"},
        json={"address": VALID_ADDRESS, "reason": "typo in apartment number"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["previous_address"]["address1"] == "123 Old Street"
    assert body["address"]["address1"] == "456 New Avenue"
    assert fake.received_address == VALID_ADDRESS

    import asyncio

    import agent.storage as storage_module

    audit = asyncio.run(storage_module.store.get_action_audit("e4"))
    assert audit["action"] == "edit_address"
    assert audit["status"] == "succeeded"
    assert audit["request"]["previous_address"]["address1"] == "123 Old Street"
    assert audit["request"]["address"]["address1"] == "456 New Avenue"

    messages = asyncio.run(storage_module.store.get_messages(ticket_id))
    assert any("Shipping address updated" in m.content for m in messages)


def test_edit_address_idempotency_replays(client):
    c, cs_module = client
    fake = FakeShopifyAddress()
    cs_module._agent.shopify = fake

    ticket_id = _create_ticket(c)
    _link_order(ticket_id)

    headers = {**AUTH, "Idempotency-Key": "e5"}
    r1 = c.post(
        f"/support/tickets/{ticket_id}/actions/edit-address",
        headers=headers,
        json={"address": VALID_ADDRESS},
    )
    r2 = c.post(
        f"/support/tickets/{ticket_id}/actions/edit-address",
        headers=headers,
        json={"address": VALID_ADDRESS},
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["replayed"] is False
    assert r2.json()["replayed"] is True
    assert fake.update_calls == 1


# ── Partial refund (line items + cumulative cap) ────────────


class FakeShopifyPartialRefund:
    enabled = True

    def __init__(self, order=None):
        self.order = order or {
            "id": "999",
            "total_price": "100.00",
            "total_refunded": "80.00",
            "line_items": [
                {"id": 111, "quantity": 2, "title": "Blue Hoodie"},
                {"id": 222, "quantity": 1, "title": "Beanie"},
            ],
        }
        self.refund_calls = []
        self.refund_kwargs = None

    async def get_order_by_id(self, order_id):
        return self.order

    async def create_refund(self, **kwargs):
        self.refund_kwargs = kwargs
        self.refund_calls.append(kwargs)
        return {"refund": {"id": 555}}


def test_partial_refund_rejects_amount_beyond_remaining_refundable(client):
    """Order totals 100, already refunded 80 → only 20 refundable. Asking 30 must fail —
    cumulative partial refunds can never drain more than the order was worth."""
    c, cs_module = client
    cs_module._agent.shopify = FakeShopifyPartialRefund()

    ticket_id = _create_ticket(c)
    _link_order(ticket_id)

    r = c.post(
        f"/support/tickets/{ticket_id}/actions/refund",
        headers={**AUTH, "Idempotency-Key": "p1"},
        json={"amount": 30.0},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "REFUND_EXCEEDS_TOTAL"
    details = r.json()["details"]
    assert details["already_refunded"] == 80.0
    assert details["refundable"] == 20.0


def test_partial_refund_with_valid_line_items_passes_them_through(client):
    c, cs_module = client
    fake = FakeShopifyPartialRefund()
    cs_module._agent.shopify = fake

    ticket_id = _create_ticket(c)
    _link_order(ticket_id)

    r = c.post(
        f"/support/tickets/{ticket_id}/actions/refund",
        headers={**AUTH, "Idempotency-Key": "p2"},
        json={
            "amount": 15.0,
            "reason": "one hoodie arrived damaged",
            "refund_line_items": [{"line_item_id": 111, "quantity": 1}],
        },
    )
    assert r.status_code == 200
    assert fake.refund_kwargs["refund_line_items"] == [{"id": 111, "quantity": 1}]

    import asyncio

    import agent.storage as storage_module

    audit = asyncio.run(storage_module.store.get_refund_audit("p2"))
    assert audit["detail"]["line_items"] == [{"line_item_id": 111, "quantity": 1}]


def test_partial_refund_unknown_line_item_rejected(client):
    c, cs_module = client
    fake = FakeShopifyPartialRefund()
    cs_module._agent.shopify = fake

    ticket_id = _create_ticket(c)
    _link_order(ticket_id)

    r = c.post(
        f"/support/tickets/{ticket_id}/actions/refund",
        headers={**AUTH, "Idempotency-Key": "p3"},
        json={
            "amount": 5.0,
            "refund_line_items": [{"line_item_id": 99999, "quantity": 1}],
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "REFUND_LINE_ITEM_NOT_FOUND"
    assert fake.refund_kwargs is None, "must not call Shopify when validation fails"


def test_partial_refund_quantity_exceeding_order_rejected(client):
    c, cs_module = client
    fake = FakeShopifyPartialRefund()
    cs_module._agent.shopify = fake

    ticket_id = _create_ticket(c)
    _link_order(ticket_id)

    r = c.post(
        f"/support/tickets/{ticket_id}/actions/refund",
        headers={**AUTH, "Idempotency-Key": "p4"},
        json={
            "amount": 10.0,
            "refund_line_items": [{"line_item_id": 111, "quantity": 5}],
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "REFUND_QUANTITY_EXCEEDS_ORDER"
    assert fake.refund_kwargs is None


def test_full_refund_without_line_items_still_works(client):
    """Regression: plain monetary refunds (no line items) must keep working exactly as
    before — and must not send a refund_line_items kwarg to Shopify at all."""
    c, cs_module = client
    fake = FakeShopifyPartialRefund(
        order={"id": "999", "total_price": "100.00", "total_refunded": "0", "line_items": []}
    )
    cs_module._agent.shopify = fake

    ticket_id = _create_ticket(c)
    _link_order(ticket_id)

    r = c.post(
        f"/support/tickets/{ticket_id}/actions/refund",
        headers={**AUTH, "Idempotency-Key": "p5"},
        json={"amount": 25.0},
    )
    assert r.status_code == 200
    assert "refund_line_items" not in fake.refund_kwargs
