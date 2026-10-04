"""Return-eligibility policy and the return-label approval endpoints.

Invariants proven here:
- The eligibility rules (window, shipped, not-already-refunded) are pure functions
  that the endpoint re-verifies server-side — the dashboard preview is advisory.
- Buying a label always needs a human POST with an Idempotency-Key; a replayed
  key NEVER buys a second paid label.
- Missing ShipEngine / return-address config fails loudly (400/409) instead of
  producing a half-configured shipment.
"""

import os
import sys
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent.rate_limit as rate_limit_module
from agent.config import settings
from agent.returns import evaluate_return_eligibility

# ── Eligibility policy (pure) ──────────────────────────────


def _order(**overrides):
    order = {
        "id": "999",
        "created_at": (datetime.now(UTC) - timedelta(days=5)).isoformat(),
        "financial_status": "paid",
        "fulfillment_status": "fulfilled",
        "line_items": [{"id": 111, "title": "Mug", "quantity": 1, "sku": "MUG-1"}],
        "shipping_address": {
            "first_name": "Ana",
            "last_name": "C",
            "address1": "1 Customer St",
            "city": "Portland",
            "province": "OR",
            "zip": "97201",
            "country": "US",
            "phone": "555-0100",
        },
    }
    order.update(overrides)
    return order


def test_eligible_within_window():
    result = evaluate_return_eligibility(_order())
    assert result["eligible"] is True
    assert result["reason"] is None
    assert len(result["line_items"]) == 1
    assert result["line_items"][0]["title"] == "Mug"
    assert result["last_return_date"] is not None


def test_window_expired_is_not_eligible():
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    result = evaluate_return_eligibility(_order(created_at=old), window_days=30)
    assert result["eligible"] is False
    assert "expired" in result["reason"]


def test_unfulfilled_order_tells_us_to_cancel_instead():
    result = evaluate_return_eligibility(_order(fulfillment_status="unfulfilled"))
    assert result["eligible"] is False
    assert "cancel" in result["reason"].lower()


def test_fully_refunded_order_is_not_eligible():
    result = evaluate_return_eligibility(_order(financial_status="refunded"))
    assert result["eligible"] is False
    assert "refunded" in result["reason"].lower()


def test_missing_order_is_not_eligible():
    result = evaluate_return_eligibility(None)
    assert result["eligible"] is False
    assert "No order" in result["reason"]


def test_unknown_order_date_does_not_silently_approve():
    result = evaluate_return_eligibility(_order(created_at=None))
    assert result["eligible"] is False
    assert "date unknown" in result["reason"].lower()


# ── Endpoints ──────────────────────────────────────────────


class FakeShipEngine:
    def __init__(self):
        pass

    enabled = True

    @property
    def configured(self):
        return (True, [])

    buy_calls = 0

    async def quote_and_buy(self, customer_address, rma_number=None, rate_id=None):
        type(self).buy_calls += 1
        type(self).last_address = customer_address
        return {
            "label_id": "L-1",
            "label_url": "https://labels.example/L-1.pdf",
            "tracking_number": "TR-1",
            "tracking_url": "https://track.example/TR-1",
            "status": "completed",
            "cost_usd": 4.35,
            "currency": "USD",
            "carrier": "USPS",
            "service_code": "usps_priority",
            "rma_number": rma_number,
        }


class FakeShipEngineDisabled:
    enabled = False

    @property
    def configured(self):
        return (False, ["SHIPENGINE_API_KEY"])


class FakeShopifyReturns:
    enabled = True
    order = None

    async def get_order_by_id(self, order_id):
        return self.order

    async def tag_order(self, order_id, tag):
        return {"id": order_id, "tagged": tag}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-key-for-tests")
    monkeypatch.setenv("TENANT_NAME", "test")
    settings.GOOGLE_API_KEY = SecretStr("dummy-key-for-tests")
    settings.DB_PATH = str(tmp_path / "test_returns.db")
    from agent.storage import store as _store

    _store.db_path = settings.DB_PATH
    settings.REQUIRE_API_KEY = True
    settings.API_KEY = SecretStr("test-key-123")
    settings.RATE_LIMIT_PER_MINUTE = 100
    settings.ACTION_RATE_LIMIT_PER_MINUTE = 100
    rate_limit_module._request_log.clear()
    monkeypatch.setattr(settings, "SHIPENGINE_API_KEY", None)
    monkeypatch.setattr(settings, "RETURN_ADDRESS_NAME", None)
    monkeypatch.setattr(settings, "RETURN_ADDRESS1", None)
    monkeypatch.setattr(settings, "RETURN_CITY", None)
    monkeypatch.setattr(settings, "RETURN_ZIP", None)

    import importlib

    import api.customer_support as cs_module
    import api.main as main_module

    importlib.reload(cs_module)
    importlib.reload(main_module)

    from tests.conftest import FakeClassifier, FakeResponseEngine

    cs_module._agent.classifier = FakeClassifier()
    cs_module._agent.response_engine = FakeResponseEngine()
    cs_module._agent.shopify = FakeShopifyReturns()
    FakeShipEngine.buy_calls = 0

    with TestClient(main_module.app, raise_server_exceptions=False) as c:
        yield c, cs_module


AUTH = {"X-API-Key": "test-key-123"}


def _create_ticket_with_order(c, order):
    r = c.post(
        "/support/tickets",
        headers=AUTH,
        json={"customer_email": "a@b.com", "subject": "return", "body": "i want to return"},
    )
    assert r.status_code == 200, r.text
    ticket_id = r.json()["ticket_id"]

    if order is not None:
        import asyncio

        import agent.storage as storage_module

        asyncio.run(storage_module.store.update_status(ticket_id, order_id=order.get("id", "999")))
    return ticket_id


def test_eligibility_endpoint_reports_eligible(client):
    c, cs_module = client
    order = _order()
    ticket_id = _create_ticket_with_order(c, order)
    cs_module._agent.shopify.order = order

    r = c.get(f"/support/tickets/{ticket_id}/return-eligibility", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["eligible"] is True
    assert body["label_provider"]["configured"] is False  # ShipEngine key absent
    assert body["label_provider"]["missing_settings"] != []


def test_eligibility_endpoint_flags_missing_order(client):
    c, _ = client
    ticket_id = _create_ticket_with_order(c, None)
    r = c.get(f"/support/tickets/{ticket_id}/return-eligibility", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["eligible"] is False
    assert "No order" in body["reason"]


def test_return_label_requires_idempotency_key(client):
    c, _ = client
    ticket_id = _create_ticket_with_order(c, _order())
    r = c.post(f"/support/tickets/{ticket_id}/actions/return-label", headers=AUTH, json={})
    assert r.status_code == 422


def test_return_label_unconfigured_shipengine_400(client):
    c, cs_module = client
    ticket_id = _create_ticket_with_order(c, _order())
    cs_module.ShipEngineClient = FakeShipEngineDisabled
    r = c.post(
        f"/support/tickets/{ticket_id}/actions/return-label",
        headers={**AUTH, "Idempotency-Key": "rl-cfg"},
        json={},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "SHIPENGINE_NOT_CONFIGURED"


def test_return_label_ineligible_order_409(client):
    c, cs_module = client
    order = _order(fulfillment_status="unfulfilled")
    ticket_id = _create_ticket_with_order(c, order)
    cs_module._agent.shopify.order = order
    cs_module.ShipEngineClient = FakeShipEngine
    r = c.post(
        f"/support/tickets/{ticket_id}/actions/return-label",
        headers={**AUTH, "Idempotency-Key": "rl-inel"},
        json={},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "RETURN_NOT_ELIGIBLE"
    assert FakeShipEngine.buy_calls == 0


def test_return_label_missing_return_address_409(client):
    c, cs_module = client
    order = _order()
    ticket_id = _create_ticket_with_order(c, order)
    cs_module._agent.shopify.order = order

    class MissingAddress(FakeShipEngine):
        @property
        def configured(self):
            return (False, ["RETURN_ADDRESS1"])

    cs_module.ShipEngineClient = MissingAddress
    r = c.post(
        f"/support/tickets/{ticket_id}/actions/return-label",
        headers={**AUTH, "Idempotency-Key": "rl-addr"},
        json={},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "RETURN_ADDRESS_MISSING"


def test_return_label_happy_path_buys_once_and_replays(client):
    c, cs_module = client
    order = _order()
    ticket_id = _create_ticket_with_order(c, order)
    cs_module._agent.shopify.order = order
    cs_module.ShipEngineClient = FakeShipEngine

    r = c.post(
        f"/support/tickets/{ticket_id}/actions/return-label",
        headers={**AUTH, "Idempotency-Key": "rl-1"},
        json={"rma_number": "RMA-77"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["replayed"] is False
    assert body["label"]["tracking_number"] == "TR-1"
    assert FakeShipEngine.buy_calls == 1
    # Return shipment goes FROM the customer address on the order.
    assert FakeShipEngine.last_address["address1"] == "1 Customer St"

    # Audit + ticket state.
    import asyncio

    import agent.storage as storage_module

    audit = asyncio.run(storage_module.store.get_return_label_audit("rl-1"))
    assert audit is not None
    assert audit["status"] == "succeeded"
    assert audit["cost_usd"] == 4.35
    ticket = asyncio.run(storage_module.store.get(ticket_id))
    assert ticket["ticket"]["status"] == "awaiting_customer"

    # Replay: same key → no second purchase.
    r2 = c.post(
        f"/support/tickets/{ticket_id}/actions/return-label",
        headers={**AUTH, "Idempotency-Key": "rl-1"},
        json={"rma_number": "RMA-77"},
    )
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["label"]["label_id"] == "L-1"
    assert FakeShipEngine.buy_calls == 1


def test_return_label_provider_failure_is_recorded(client):
    c, cs_module = client
    order = _order()
    ticket_id = _create_ticket_with_order(c, order)
    cs_module._agent.shopify.order = order

    class FailingShipEngine(FakeShipEngine):
        async def quote_and_buy(self, customer_address, rma_number=None, rate_id=None):
            from api.errors import APIError

            raise APIError(code="SHIPENGINE_ERROR", message="carrier down", status=502)

    cs_module.ShipEngineClient = FailingShipEngine
    r = c.post(
        f"/support/tickets/{ticket_id}/actions/return-label",
        headers={**AUTH, "Idempotency-Key": "rl-fail"},
        json={},
    )
    assert r.status_code == 502
    import asyncio

    import agent.storage as storage_module

    audit = asyncio.run(storage_module.store.get_return_label_audit("rl-fail"))
    assert audit is not None
    assert audit["status"] == "failed"
    assert "carrier down" in audit["error"]
