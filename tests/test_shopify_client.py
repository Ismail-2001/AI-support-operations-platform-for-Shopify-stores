"""ShopifyClient.create_refund payload correctness.

Caught live (2026-10-07): on an order whose sale transaction is in CAD while
the shop currency is USD, Shopify rejected the refund with 422 "Currency must
match parent transaction CAD" because the refund transaction omitted `currency`.
All prior tests mocked create_refund entirely, so the payload never shipped
through a test."""

import typing

import pytest

import integrations.shopify as shopify_module
from integrations.shopify import ShopifyClient

SALE_TXN = {
    "id": 8080336748729,
    "kind": "sale",
    "gateway": "manual",
    "currency": "CAD",
    "amount": "2146.00",
}


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient; records the refund POST."""

    instances: typing.ClassVar[list] = []

    def __init__(self, *args, **kwargs):
        self.posted = None
        _FakeAsyncClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        return _FakeResp({"transactions": [dict(SALE_TXN)]})

    async def post(self, url, headers=None, json=None):
        self.posted = {"url": url, "json": json}
        return _FakeResp({"refund": {"id": 1}})


@pytest.fixture
def shopify_client(monkeypatch):
    _FakeAsyncClient.instances.clear()
    monkeypatch.setattr(shopify_module.httpx, "AsyncClient", _FakeAsyncClient)
    return ShopifyClient(shop_domain="test.myshopify.com", access_token="tok")


async def test_refund_transaction_carries_parent_currency(shopify_client):
    await shopify_client.create_refund(
        order_id="6535754842297", amount=1.0, reason="r", notify_customer=False
    )
    posted = _FakeAsyncClient.instances[-1].posted
    txn = posted["json"]["refund"]["transactions"][0]
    assert txn["currency"] == "CAD"
    assert txn["parent_id"] == SALE_TXN["id"]
    assert txn["amount"] == "1.00"
    assert txn["kind"] == "refund"
    assert txn["gateway"] == "manual"
    assert posted["json"]["refund"]["notify"] is False


async def test_refund_omits_currency_when_parent_has_none(shopify_client, monkeypatch):
    no_currency = {k: v for k, v in SALE_TXN.items() if k != "currency"}

    class _NoCurrencyClient(_FakeAsyncClient):
        async def get(self, url, headers=None):
            return _FakeResp({"transactions": [no_currency]})

    monkeypatch.setattr(shopify_module.httpx, "AsyncClient", _NoCurrencyClient)
    await shopify_client.create_refund(order_id="1", amount=2.5, reason="r")
    txn = _FakeAsyncClient.instances[-1].posted["json"]["refund"]["transactions"][0]
    assert "currency" not in txn
    assert txn["amount"] == "2.50"


async def test_refund_without_sale_transaction_raises(shopify_client, monkeypatch):
    class _NoSaleClient(_FakeAsyncClient):
        async def get(self, url, headers=None):
            return _FakeResp({"transactions": [{"id": 1, "kind": "authorization"}]})

    monkeypatch.setattr(shopify_module.httpx, "AsyncClient", _NoSaleClient)
    with pytest.raises(ValueError, match="No sale transaction"):
        await shopify_client.create_refund(order_id="1", amount=1.0)
