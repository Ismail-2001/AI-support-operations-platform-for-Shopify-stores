"""Subscription management: Recharge + Skio clients, the SubscriptionService facade,
and the human-approved action endpoints.

Invariants proven here:
- Nothing executes without a human POSTing an Idempotency-Key; replays never
  re-execute (pausing twice / cancelling twice are customer-visible mistakes).
- Shared state guards hold across providers: no touching a cancelled sub, no skip
  without an upcoming charge, frequency limited to day/week/month 1-60.
- Skio never parses customer input into GraphQL (literal-embedded queries only).
- An unconnected provider degrades to a clear 409 / configured:false — never to
  a fabricated subscription state.
"""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent.rate_limit as rate_limit_module
from agent.config import settings
from integrations.recharge import RechargeClient
from integrations.skio import SkioClient
from integrations.subscriptions import (
    NormalizedSubscription,
    SubscriptionAPIError,
    SubscriptionInvalidState,
    SubscriptionNotConfigured,
    SubscriptionNotFound,
    SubscriptionService,
)


def _patch_http(monkeypatch, handler):
    """Route every httpx.AsyncClient the module builds through a MockTransport."""
    original_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.fixture
def recharge_tokens(monkeypatch):
    monkeypatch.setattr(settings, "RECHARGE_API_TOKEN", SecretStr("rc_test_token"))
    monkeypatch.setattr(settings, "SUBSCRIPTION_PROVIDER", "auto")
    monkeypatch.setattr(settings, "SUBSCRIPTION_PAUSE_DAYS", 30)


@pytest.fixture
def skio_tokens(monkeypatch):
    monkeypatch.setattr(settings, "SKIO_API_TOKEN", SecretStr("sk_test_token"))
    monkeypatch.setattr(settings, "SUBSCRIPTION_PROVIDER", "auto")


@pytest.fixture
def no_provider(monkeypatch):
    monkeypatch.setattr(settings, "RECHARGE_API_TOKEN", None)
    monkeypatch.setattr(settings, "SKIO_API_TOKEN", None)
    monkeypatch.setattr(settings, "SUBSCRIPTION_PROVIDER", "auto")


def _sub_payload(**overrides):
    sub = {
        "id": 123,
        "status": "active",
        "customer_id": 7,
        "address_id": 9,
        "product_title": "Coffee Beans",
        "quantity": 2,
        "price": "20.00",
        "next_charge_scheduled_at": "2026-10-20",
        "order_interval_unit": "month",
        "order_interval_frequency": 1,
        "charge_interval_frequency": 1,
    }
    sub.update(overrides)
    return sub


# ── Recharge client ────────────────────────────────────────


def test_recharge_list_subscriptions_normalizes(recharge_tokens, monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.url.path == "/customers":
            assert request.headers["X-Recharge-Access-Token"] == "rc_test_token"
            return httpx.Response(200, json={"customers": [{"id": 7, "email": "a@b.com"}]})
        if request.url.path == "/subscriptions":
            return httpx.Response(200, json={"subscriptions": [_sub_payload()]})
        if request.url.path == "/addresses/9":
            return httpx.Response(
                200,
                json={
                    "id": 9,
                    "first_name": "Ada",
                    "last_name": "L",
                    "address1": "1 Main St",
                    "city": "Springfield",
                    "province": "IL",
                    "zip": "62704",
                    "country": "US",
                },
            )
        return httpx.Response(404, json={})

    _patch_http(monkeypatch, handler)
    subs = asyncio.run(RechargeClient().list_subscriptions("a@b.com"))
    assert len(subs) == 1
    sub = subs[0]
    assert sub.id == "123"
    assert sub.provider == "recharge"
    assert sub.frequency_unit == "month"
    assert sub.frequency_count == 1
    assert sub.address["state"] == "IL"  # Recharge's province -> normalized state
    assert sub.email == "a@b.com"


def test_recharge_pause_pushes_next_charge_out(recharge_tokens, monkeypatch):
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["method"] = request.method
        sent["path"] = request.url.path
        sent["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"subscription": {"id": 123}})

    _patch_http(monkeypatch, handler)
    client = RechargeClient()
    sub = NormalizedSubscription(
        id="123", provider="recharge", status="ACTIVE", title="Coffee", raw={"address_id": 9}
    )
    asyncio.run(client.pause(sub))

    # No pause endpoint exists — pause = reschedule the next charge.
    assert sent["method"] == "POST"
    assert sent["path"] == "/subscriptions/123/change_next_charge_date"
    expected = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
    assert sent["body"]["date"] == expected


def test_recharge_skip_targets_address_charge(recharge_tokens, monkeypatch):
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["path"] = request.url.path
        sent["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"charge": {"id": 1}})

    _patch_http(monkeypatch, handler)
    sub = NormalizedSubscription(
        id="123",
        provider="recharge",
        status="ACTIVE",
        title="Coffee",
        next_charge_date="2026-10-20T08:00:00",
        raw={"address_id": 9},
    )
    asyncio.run(RechargeClient().skip(sub))
    assert sent["path"] == "/addresses/9/charges/skip"
    assert sent["body"]["date"] == "2026-10-20"


def test_recharge_missing_subscription_maps_to_not_found(recharge_tokens, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/customers":
            return httpx.Response(200, json={"customers": []})
        if request.url.path.startswith("/subscriptions"):
            return httpx.Response(404, json={})
        return httpx.Response(404, json={})

    _patch_http(monkeypatch, handler)
    client = RechargeClient()
    assert asyncio.run(client.get_subscription("999")) is None
    # list with unknown customer -> empty, not an error
    assert asyncio.run(client.list_subscriptions("nobody@x.com")) == []


def test_recharge_4xx_raises_without_retry(recharge_tokens, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "bad request"})

    _patch_http(monkeypatch, handler)
    client = RechargeClient()
    with pytest.raises(SubscriptionAPIError):
        asyncio.run(client.get_subscription("123"))


# ── Skio client ────────────────────────────────────────────


def _skio_sub():
    return {
        "id": "gid://shopify/SkioSubscription/55",
        "status": "ACTIVE",
        "nextBillingDate": "2026-11-01",
        "StorefrontUser": {"email": "a@b.com"},
        "ShippingAddress": {
            "address1": "2 Oak Ave",
            "address2": "",
            "city": "Portland",
            "province": "OR",
            "zip": "97201",
            "country": "US",
            "firstName": "Ada",
            "lastName": "L",
            "phoneNumber": "555",
        },
        "BillingPolicy": {"interval": "MONTH", "intervalCount": 1},
        "SubscriptionLines": [
            {
                "quantity": 1,
                "priceWithoutDiscount": "15.00",
                "ProductVariant": {
                    "title": "340g",
                    "Product": {"title": "Espresso"},
                },
            }
        ],
    }


def test_skio_list_subscriptions_uses_application_graphql(skio_tokens, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["content_type"] = request.headers.get("content-type")
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"data": {"Subscriptions": [_skio_sub()]}})

    _patch_http(monkeypatch, handler)
    subs = asyncio.run(SkioClient().list_subscriptions('a"b@evil.com'))
    assert seen["path"] == "/v1/graphql"
    assert seen["content_type"] == "application/graphql"
    assert seen["authorization"] == "API sk_test_token"
    # Customer input is literal-embedded as JSON — quotes can't break the query.
    assert '"a\\"b@evil.com"' in seen["body"]

    assert len(subs) == 1
    sub = subs[0]
    assert sub.id == "gid://shopify/SkioSubscription/55"
    assert sub.provider == "skio"
    assert sub.title == "Espresso — 340g"
    assert sub.frequency_unit == "month"
    assert sub.next_charge_date == "2026-11-01"
    assert sub.address["state"] == "OR"


def test_skio_pause_sends_literal_subscription_id(skio_tokens, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"data": {"pauseSubscription": {"ok": True}}})

    _patch_http(monkeypatch, handler)
    sub = NormalizedSubscription(id="sub-1", provider="skio", status="ACTIVE", title="t")
    asyncio.run(SkioClient().pause(sub))
    assert 'pauseSubscription(input: {subscriptionId: "sub-1"})' in seen["body"]


def test_skio_graphql_errors_raise_api_error(skio_tokens, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "permission denied"}]})

    _patch_http(monkeypatch, handler)
    with pytest.raises(SubscriptionAPIError, match="permission denied"):
        asyncio.run(SkioClient().get_subscription("x"))


# ── SubscriptionService facade ─────────────────────────────


def test_service_not_configured_raises(no_provider):
    service = SubscriptionService()
    assert service.enabled is False
    with pytest.raises(SubscriptionNotConfigured):
        asyncio.run(service.list_subscriptions("a@b.com"))


def test_service_auto_picks_recharge_when_both_configured(monkeypatch):
    monkeypatch.setattr(settings, "RECHARGE_API_TOKEN", SecretStr("rc"))
    monkeypatch.setattr(settings, "SKIO_API_TOKEN", SecretStr("sk"))
    monkeypatch.setattr(settings, "SUBSCRIPTION_PROVIDER", "auto")
    assert SubscriptionService().provider == "recharge"
    monkeypatch.setattr(settings, "SUBSCRIPTION_PROVIDER", "skio")
    assert SubscriptionService().provider == "skio"


class _FakeClient:
    def __init__(self, sub):
        self.sub = sub
        self.calls = []

    async def get_subscription(self, subscription_id):
        return self.sub

    async def pause(self, sub):
        self.calls.append("pause")

    async def skip(self, sub):
        self.calls.append("skip")

    async def cancel(self, sub, reason):
        self.calls.append("cancel")

    async def update_address(self, sub, address):
        self.calls.append("update_address")

    async def change_frequency(self, sub, unit, count):
        self.calls.append("change_frequency")


def _service_with(sub, provider="recharge"):
    service = SubscriptionService.__new__(SubscriptionService)
    service.requested = provider
    service.provider = provider
    service._client = _FakeClient(sub)
    return service


def test_service_blocks_cancelling_cancelled_subscription():
    sub = NormalizedSubscription(id="1", provider="recharge", status="CANCELLED", title="t")
    service = _service_with(sub)
    with pytest.raises(SubscriptionInvalidState):
        asyncio.run(service.pause("1"))
    with pytest.raises(SubscriptionInvalidState):
        asyncio.run(service.cancel("1"))


def test_service_blocks_skip_without_upcoming_charge():
    sub = NormalizedSubscription(id="1", provider="recharge", status="ACTIVE", title="t")
    service = _service_with(sub)
    with pytest.raises(SubscriptionInvalidState, match="no upcoming charge"):
        asyncio.run(service.skip("1"))


def test_service_frequency_unit_and_count_guards():
    sub = NormalizedSubscription(id="1", provider="recharge", status="ACTIVE", title="t")
    service = _service_with(sub)
    with pytest.raises(SubscriptionInvalidState, match="Unsupported frequency"):
        asyncio.run(service.change_frequency("1", "fortnight", 1))
    with pytest.raises(SubscriptionInvalidState, match="out of range"):
        asyncio.run(service.change_frequency("1", "month", 0))
    with pytest.raises(SubscriptionInvalidState, match="out of range"):
        asyncio.run(service.change_frequency("1", "day", 61))
    # Valid call goes through with a normalized unit.
    asyncio.run(service.change_frequency("1", "WEEK", 2))
    assert service._client.calls == ["change_frequency"]


def test_service_get_missing_subscription_raises_not_found():
    service = SubscriptionService.__new__(SubscriptionService)
    service.requested = "recharge"
    service.provider = "recharge"
    service._client = _FakeClient(None)  # provider says the subscription doesn't exist
    with pytest.raises(SubscriptionNotFound):
        asyncio.run(service.get_subscription("404"))


# ── Endpoints ──────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-key-for-tests")
    monkeypatch.setenv("TENANT_NAME", "test")
    settings.GOOGLE_API_KEY = SecretStr("dummy-key-for-tests")
    settings.DB_PATH = str(tmp_path / "test_subs.db")
    from agent.storage import store as _store

    _store.db_path = settings.DB_PATH
    settings.REQUIRE_API_KEY = True
    settings.API_KEY = SecretStr("test-key-123")
    settings.RATE_LIMIT_PER_MINUTE = 100
    settings.ACTION_RATE_LIMIT_PER_MINUTE = 100
    rate_limit_module._request_log.clear()
    monkeypatch.setattr(settings, "RECHARGE_API_TOKEN", None)
    monkeypatch.setattr(settings, "SKIO_API_TOKEN", None)
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

    from tests.conftest import FakeClassifier, FakeResponseEngine, FakeShopify

    cs_module._agent.classifier = FakeClassifier()
    cs_module._agent.response_engine = FakeResponseEngine()
    cs_module._agent.shopify = FakeShopify()

    with TestClient(main_module.app, raise_server_exceptions=False) as c:
        yield c, cs_module


AUTH = {"X-API-Key": "test-key-123"}


def _create_ticket(c, email="a@b.com"):
    r = c.post(
        "/support/tickets",
        headers=AUTH,
        json={"customer_email": email, "subject": "sub please", "body": "change my sub"},
    )
    assert r.status_code == 200, r.text
    return r.json()["ticket_id"]


class _FakeService:
    instances: ClassVar[list] = []
    calls: ClassVar[list[str]] = []

    def __init__(self, provider=None):
        self.provider = provider or "recharge"
        self.enabled = True
        self.pause_calls = 0
        _FakeService.instances.append(self)

    async def list_subscriptions(self, email):
        return [
            NormalizedSubscription(id="sub-1", provider="recharge", status="ACTIVE", title="Coffee")
        ]

    def _ok(self, subscription_id, status="ACTIVE", **extra):
        return NormalizedSubscription(
            id=subscription_id, provider="recharge", status=status, title="Coffee", **extra
        )

    async def pause(self, subscription_id):
        self.pause_calls += 1
        return self._ok(subscription_id)

    async def skip(self, subscription_id):
        _FakeService.calls.append(f"skip:{subscription_id}")
        return self._ok(subscription_id)

    async def cancel(self, subscription_id, reason="Requested by customer"):
        _FakeService.calls.append(f"cancel:{subscription_id}")
        return self._ok(subscription_id, status="CANCELLED")

    async def update_address(self, subscription_id, address):
        _FakeService.calls.append(f"address:{subscription_id}")
        return self._ok(subscription_id)

    async def change_frequency(self, subscription_id, unit, count):
        _FakeService.calls.append(f"freq:{subscription_id}:{unit}:{count}")
        return self._ok(subscription_id)


def test_get_subscriptions_unconfigured_returns_configured_false(client):
    c, _ = client
    ticket_id = _create_ticket(c)
    r = c.get(f"/support/tickets/{ticket_id}/subscriptions", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["subscriptions"] == []


def test_get_subscriptions_lists_from_provider(client):
    c, cs_module = client
    ticket_id = _create_ticket(c)
    _FakeService.instances.clear()
    cs_module.SubscriptionService = _FakeService
    r = c.get(f"/support/tickets/{ticket_id}/subscriptions", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert body["subscriptions"][0]["id"] == "sub-1"
    assert "raw" not in body["subscriptions"][0]


def test_subscription_action_requires_idempotency_key(client):
    c, _ = client
    ticket_id = _create_ticket(c)
    r = c.post(
        f"/support/tickets/{ticket_id}/actions/subscription",
        headers=AUTH,
        json={"subscription_id": "sub-1", "operation": "pause"},
    )
    assert r.status_code == 422


def test_subscription_action_unknown_provider_rejected(client):
    c, _ = client
    ticket_id = _create_ticket(c)
    r = c.post(
        f"/support/tickets/{ticket_id}/actions/subscription",
        headers={**AUTH, "Idempotency-Key": "k-provider"},
        json={"subscription_id": "sub-1", "operation": "pause", "provider": "bold"},
    )
    assert r.status_code == 422


def test_subscription_action_pause_executes_and_replays(client):
    c, cs_module = client
    ticket_id = _create_ticket(c)
    _FakeService.instances.clear()
    cs_module.SubscriptionService = _FakeService

    payload = {"subscription_id": "sub-1", "operation": "pause", "reason": "vacation"}
    r = c.post(
        f"/support/tickets/{ticket_id}/actions/subscription",
        headers={**AUTH, "Idempotency-Key": "sub-k1"},
        json=payload,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["replayed"] is False
    assert body["operation"] == "pause"
    assert body["subscription"]["id"] == "sub-1"

    # Audit trail exists with the operation name.
    import agent.storage as storage_module

    audit = asyncio.run(storage_module.store.get_action_audit("sub-k1"))
    assert audit is not None
    assert audit["action"] == "subscription_pause"

    # Replay: same key, no second execution (no new service is even constructed).
    r2 = c.post(
        f"/support/tickets/{ticket_id}/actions/subscription",
        headers={**AUTH, "Idempotency-Key": "sub-k1"},
        json=payload,
    )
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert len(_FakeService.instances) == 1


def test_subscription_action_rejects_conflicting_idempotency_key(client):
    c, cs_module = client
    ticket_id = _create_ticket(c)
    _FakeService.instances.clear()
    cs_module.SubscriptionService = _FakeService
    import agent.storage as storage_module

    asyncio.run(
        storage_module.store.record_action_audit(
            "conflict-key", ticket_id, "", "cancel_order", {}, status="succeeded"
        )
    )
    r = c.post(
        f"/support/tickets/{ticket_id}/actions/subscription",
        headers={**AUTH, "Idempotency-Key": "conflict-key"},
        json={"subscription_id": "sub-1", "operation": "pause"},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_subscription_action_nonexistent_ticket_404(client):
    c, cs_module = client
    _FakeService.instances.clear()
    cs_module.SubscriptionService = _FakeService
    r = c.post(
        "/support/tickets/nope/actions/subscription",
        headers={**AUTH, "Idempotency-Key": "sub-missing"},
        json={"subscription_id": "sub-1", "operation": "pause"},
    )
    assert r.status_code == 404


def test_subscription_action_frequency_requires_frequency_object(client):
    c, cs_module = client
    ticket_id = _create_ticket(c)
    _FakeService.instances.clear()
    cs_module.SubscriptionService = _FakeService
    r = c.post(
        f"/support/tickets/{ticket_id}/actions/subscription",
        headers={**AUTH, "Idempotency-Key": "sub-freq"},
        json={"subscription_id": "sub-1", "operation": "change_frequency"},
    )
    assert r.status_code == 422


def test_subscription_action_update_address_requires_address(client):
    c, cs_module = client
    ticket_id = _create_ticket(c)
    _FakeService.instances.clear()
    cs_module.SubscriptionService = _FakeService
    r = c.post(
        f"/support/tickets/{ticket_id}/actions/subscription",
        headers={**AUTH, "Idempotency-Key": "sub-addr"},
        json={"subscription_id": "sub-1", "operation": "update_address"},
    )
    assert r.status_code == 422
    r2 = c.post(
        f"/support/tickets/{ticket_id}/actions/subscription",
        headers={**AUTH, "Idempotency-Key": "sub-addr2"},
        json={
            "subscription_id": "sub-1",
            "operation": "update_address",
            "address": {"city": "Missing bits"},
        },
    )
    assert r2.status_code == 422  # pydantic validator: needs address1/city/country/zip


# ── Action execution, failure mapping, idempotency families ─────────


def test_subscription_action_degraded_provider_maps_to_409_and_audits(client):
    """No Recharge/Skio connected: the action endpoint must return the documented
    409 SUBSCRIPTION_NOT_CONNECTED (never a 500) AND leave a failed audit row."""
    c, _cs_module = client
    ticket_id = _create_ticket(c)
    # Real SubscriptionService (fixture cleared both tokens) - nothing patched.

    key = "sub-degraded"
    r = c.post(
        f"/support/tickets/{ticket_id}/actions/subscription",
        headers={**AUTH, "Idempotency-Key": key},
        json={"subscription_id": "sub-1", "operation": "pause"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "SUBSCRIPTION_NOT_CONNECTED"

    import asyncio

    import agent.storage as storage_module

    audit = asyncio.run(storage_module.store.get_action_audit(key))
    assert audit is not None
    assert audit["action"] == "subscription_pause"
    assert audit["status"] == "failed"
    assert "connected" in audit["error"].lower()

    # Replaying the failed key returns the stored failure with the ORIGINAL
    # error status — never HTTP 200 (a failed action must not replay as OK).
    r2 = c.post(
        f"/support/tickets/{ticket_id}/actions/subscription",
        headers={**AUTH, "Idempotency-Key": key},
        json={"subscription_id": "sub-1", "operation": "pause"},
    )
    assert r2.status_code == 409
    assert r2.json()["error"] == "SUBSCRIPTION_NOT_CONNECTED"
    assert r2.json()["details"]["replayed"] is True
    assert "connected" in r2.json()["message"].lower()


def test_subscription_guard_failure_maps_to_409_and_audits(client):
    c, cs_module = client
    ticket_id = _create_ticket(c)
    _FakeService.calls.clear()

    from integrations.subscriptions import SubscriptionInvalidState

    class _GuardedService(_FakeService):
        async def cancel(self, subscription_id, reason="Requested by customer"):
            raise SubscriptionInvalidState("already cancelled")

    cs_module.SubscriptionService = _GuardedService
    key = "sub-guarded"
    r = c.post(
        f"/support/tickets/{ticket_id}/actions/subscription",
        headers={**AUTH, "Idempotency-Key": key},
        json={"subscription_id": "sub-1", "operation": "cancel"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "SUBSCRIPTION_INVALID_STATE"

    import asyncio

    import agent.storage as storage_module

    audit = asyncio.run(storage_module.store.get_action_audit(key))
    assert audit["status"] == "failed"
    assert "already cancelled" in audit["error"]
    assert _FakeService.calls == []  # guard fired before any provider call


def test_subscription_remaining_operations_execute_end_to_end(client):
    """skip / cancel / update_address / change_frequency all execute, each with
    its own audit action name - not just pause."""
    c, cs_module = client
    ticket_id = _create_ticket(c)
    _FakeService.calls.clear()
    _FakeService.instances.clear()
    cs_module.SubscriptionService = _FakeService

    import asyncio

    import agent.storage as storage_module

    cases = [
        ("sub-skip", {"operation": "skip"}, "subscription_skip", "skip:sub-1"),
        ("sub-cancel", {"operation": "cancel"}, "subscription_cancel", "cancel:sub-1"),
        (
            "sub-addr",
            {
                "operation": "update_address",
                "address": {
                    "address1": "9 New Rd",
                    "city": "Austin",
                    "country": "US",
                    "zip": "78701",
                },
            },
            "subscription_update_address",
            "address:sub-1",
        ),
        (
            "sub-freq-ok",
            {"operation": "change_frequency", "frequency": {"unit": "week", "count": 2}},
            "subscription_change_frequency",
            "freq:sub-1:week:2",
        ),
    ]
    for key, payload, audit_action, expected_call in cases:
        r = c.post(
            f"/support/tickets/{ticket_id}/actions/subscription",
            headers={**AUTH, "Idempotency-Key": key},
            json={"subscription_id": "sub-1", **payload},
        )
        assert r.status_code == 200, f"{key}: {r.text}"
        assert r.json()["replayed"] is False
        audit = asyncio.run(storage_module.store.get_action_audit(key))
        assert audit is not None and audit["action"] == audit_action
        assert audit["status"] == "succeeded"
        assert _FakeService.calls[-1] == expected_call
    assert _FakeService.calls == [expected for *_, expected in cases]


def test_subscription_action_rejects_cross_family_refund_key(client):
    """A refund Idempotency-Key must not be spendable on a subscription action."""
    c, _cs = client
    ticket_id = _create_ticket(c)

    import asyncio

    import agent.storage as storage_module

    asyncio.run(
        storage_module.store.record_refund_audit(
            "shared-key", ticket_id, "ord-1", 10.0, "why", status="succeeded"
        )
    )
    r = c.post(
        f"/support/tickets/{ticket_id}/actions/subscription",
        headers={**AUTH, "Idempotency-Key": "shared-key"},
        json={"subscription_id": "sub-1", "operation": "pause"},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert r.json()["details"]["table"] == "refund_audit"


# ── Store-scoped provider tokens (agency mode) ─────────────────────


def test_provider_clients_honor_store_scoped_tokens(monkeypatch):
    monkeypatch.setattr(settings, "RECHARGE_API_TOKEN", SecretStr("env-token"))
    monkeypatch.setattr(settings, "SKIO_API_TOKEN", None)

    # Outside a store scope the env token applies, as before.
    assert RechargeClient().enabled is True
    assert RechargeClient().headers["X-Recharge-Access-Token"] == "env-token"

    # Explicit store-scoped token wins...
    scoped = RechargeClient(api_token="store-token")
    assert scoped.enabled is True
    assert scoped.headers["X-Recharge-Access-Token"] == "store-token"

    # ...and an empty store token disables the client - NO env fallback.
    disabled = RechargeClient(api_token="")
    assert disabled.enabled is False

    assert SkioClient(api_token="sk-store").enabled is True
    assert SkioClient(api_token="").enabled is False


def test_subscription_service_resolves_tokens_per_store(monkeypatch):
    from agent import multistore

    monkeypatch.setattr(settings, "RECHARGE_API_TOKEN", SecretStr("env-token"))
    monkeypatch.setattr(settings, "SKIO_API_TOKEN", None)
    monkeypatch.setattr(settings, "SUBSCRIPTION_PROVIDER", "auto")

    # Outside store scope: env Recharge wins.
    assert SubscriptionService().provider == "recharge"

    def in_store(store_id, credentials):
        multistore._store_records[store_id] = {"credentials": credentials}
        token = multistore.current_store_id.set(store_id)
        try:
            return SubscriptionService()
        finally:
            multistore.current_store_id.reset(token)
            multistore._store_records.pop(store_id, None)

    # Store scoped to Skio only: env Recharge must NOT leak in.
    svc = in_store("store-skio", {"skio_api_token": "sk-1"})
    assert svc.enabled is True
    assert svc.provider == "skio"

    # Store with no subscription credentials: nothing is connected.
    svc = in_store("store-none", {})
    assert svc.enabled is False
    assert svc.provider is None

    # Explicit per-store provider override.
    svc = in_store(
        "store-both",
        {"recharge_api_token": "rc-1", "skio_api_token": "sk-1", "subscription_provider": "skio"},
    )
    assert svc.provider == "skio"
