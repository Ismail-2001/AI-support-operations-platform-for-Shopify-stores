"""Product knowledge tests: rich document building, Shopify Link-header
pagination, incremental (hash-skip) sync, pruning, and the sync/live-stock
endpoints. Embeddings are faked deterministically; no network anywhere."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

import agent.knowledge_base as kb_module
import agent.product_sync as ps
from agent.product_knowledge import (
    build_product_document,
    product_source,
    stock_snapshot,
    stock_status,
)
from integrations.shopify import _parse_page_info

_VOCAB = ["linen", "shirt", "wash", "cotton", "shipping", "refund", "care", "medium"]


async def _fake_embed(text: str):
    t = text.lower()
    vec = np.array([1.0 if w in t else 0.0 for w in _VOCAB])
    if vec.sum() == 0:
        vec = np.ones(len(_VOCAB)) * 0.01
    return vec.tolist()


@pytest.fixture(autouse=True)
def patch_embeddings(monkeypatch):
    monkeypatch.setattr(kb_module, "embed_text", _fake_embed)
    _ps_reset()
    yield
    _ps_reset()


def _ps_reset():
    ps._state.update(
        status="idle",
        force=False,
        products_seen=0,
        products_updated=0,
        products_skipped=0,
        products_failed=0,
        policies_updated=0,
        chunks_added=0,
        error=None,
        started_at=None,
        finished_at=None,
    )


def _product(handle="linen-shirt", title="Linen Shirt", price="40.00", qty=12, pid=777):
    return {
        "id": pid,
        "handle": handle,
        "title": title,
        "vendor": "Acme",
        "product_type": "Shirt",
        "tags": ["summer"],
        "body_html": "<p>Very <b>breathable</b> &amp; light.</p>",
        "variants": [{"title": "M", "price": price, "inventory_quantity": qty}],
        "images": [{"alt": "Model on a beach"}],
    }


# ── Document building ────────────────────────────────────────


def test_document_includes_every_shopper_facing_field():
    doc = build_product_document(
        _product(),
        [
            {"key": "care_instructions", "value": "Machine wash cold"},
            {"key": "material", "value": "100% linen"},
        ],
    )
    assert "Linen Shirt" in doc
    assert "breathable & light" in doc
    assert "Product ID: 777" in doc
    assert "M — $40.00 — In stock (12 available)" in doc
    assert "Care instructions: Machine wash cold" in doc
    assert "Materials: 100% linen" in doc
    assert "Image descriptions: Model on a beach" in doc


def test_document_strips_html_from_description_and_metafields():
    doc = build_product_document(
        _product(), [{"key": "size_guide", "value": "<p>Fits <em>true</em> to size</p>"}]
    )
    assert "<p>" not in doc and "<b>" not in doc and "&amp;" not in doc
    assert "Fits true to size" in doc


def test_document_is_deterministic_for_hash_skip():
    meta = [{"key": "care", "value": "Wash cold"}]
    assert build_product_document(_product(), meta) == build_product_document(_product(), meta)


def test_document_omits_missing_optional_sections():
    doc = build_product_document({"handle": "x", "title": "Bare"}, None)
    assert doc == "Bare"
    assert "Variants" not in doc and "Image" not in doc and "Vendor" not in doc


def test_metafield_unknown_key_is_humanized():
    doc = build_product_document(_product(), [{"key": "country-of-origin", "value": "Portugal"}])
    assert "Country of origin: Portugal" in doc  # hyphens normalize -> known label


def test_stock_status_thresholds():
    assert stock_status(None) is None
    assert stock_status(0) == "Out of stock"
    assert stock_status(3) == "Low stock (3 left)"
    assert stock_status(6) == "In stock (6 available)"


def test_stock_snapshot_statuses():
    snap = stock_snapshot(
        {
            "variants": [
                {"title": "S", "inventory_quantity": 9},
                {"title": "M", "inventory_quantity": 2},
                {"title": "L", "inventory_quantity": 0},
                {"title": "XL"},
            ]
        }
    )
    assert [v["status"] for v in snap] == ["in_stock", "low_stock", "out_of_stock", "unknown"]


def test_product_source_prefers_handle_falls_back_to_id():
    assert product_source(_product()) == "product:linen-shirt"
    assert product_source({"id": 5, "handle": None}) == "product:5"


# ── Shopify pagination cursor ────────────────────────────────


def test_parse_page_info_takes_next_cursor():
    link = (
        '<https://x/products.json?page_info=abc%3D123&limit=250>; rel="next", '
        '<https://x/products.json?page_info=zzz&limit=250>; rel="previous"'
    )
    assert _parse_page_info(link) == "abc=123"


def test_parse_page_info_none_when_single_page_or_previous_only():
    assert _parse_page_info(None) is None
    assert _parse_page_info('<https://x?page_info=abc>; rel="previous"') is None


# ── KB source hashes ─────────────────────────────────────────


async def test_source_hash_set_get_and_clear_on_delete(test_kb):
    await test_kb.ingest("product:p1", "P1", "Cotton shirt wash care medium")
    await test_kb.set_source_hash("product:p1", "abc")
    assert await test_kb.get_source_hash("product:p1") == "abc"
    assert await test_kb.list_sources(prefix="product:") == ["product:p1"]
    await test_kb.delete_source("product:p1")
    assert await test_kb.get_source_hash("product:p1") is None
    assert await test_kb.list_sources(prefix="product:") == []


async def test_get_source_hash_unknown_source_is_none(test_kb):
    assert await test_kb.get_source_hash("product:never-seen") is None


# ── Incremental sync (core job pieces) ───────────────────────


class _FakeShopify:
    enabled = True

    def __init__(self, products=None, policies=None):
        self.products = products if products is not None else [_product()]
        self.policies = (
            policies if policies is not None else {"Shipping Policy": "Ships in 5-7 days."}
        )

    async def get_shop_policies(self):
        return self.policies

    async def get_products(self, limit=50):
        return self.products[:limit]

    async def get_product_metafields(self, product_id):
        return [{"key": "care_instructions", "value": "Wash cold"}]

    async def get_product_by_handle(self, handle):
        return next((p for p in self.products if p["handle"] == handle), None)


async def test_sync_product_skips_unchanged_and_updates_changed(test_kb):
    fake = _FakeShopify()
    await ps._sync_product(test_kb, fake, _product(), force=False)
    assert ps._state["products_updated"] == 1
    chunks_after_first = await test_kb.count()

    await ps._sync_product(test_kb, fake, _product(), force=False)
    assert ps._state["products_skipped"] == 1
    assert ps._state["products_updated"] == 1
    assert await test_kb.count() == chunks_after_first, "unchanged product must not re-ingest"

    await ps._sync_product(test_kb, fake, _product(price="45.00"), force=False)
    assert ps._state["products_updated"] == 2


async def test_sync_product_force_reingests(test_kb):
    fake = _FakeShopify()
    await ps._sync_product(test_kb, fake, _product(), force=False)
    await ps._sync_product(test_kb, fake, _product(), force=True)
    assert ps._state["products_updated"] == 2


async def test_sync_product_counts_failures_without_crashing(test_kb):
    class _Broken(_FakeShopify):
        async def get_product_metafields(self, product_id):
            raise RuntimeError("boom")

    await ps._sync_product(test_kb, _Broken(), _product(), force=False)
    assert ps._state["products_failed"] == 1
    assert ps._state["products_updated"] == 0


async def test_sync_policy_hash_skip(test_kb):
    await ps._sync_policy(test_kb, "Shipping Policy", "Ships in 5-7 days.", force=False)
    assert ps._state["policies_updated"] == 1
    await ps._sync_policy(test_kb, "Shipping Policy", "Ships in 5-7 days.", force=False)
    assert ps._state["policies_updated"] == 1  # unchanged
    await ps._sync_policy(test_kb, "Shipping Policy", "Ships in 3 days.", force=False)
    assert ps._state["policies_updated"] == 2


# ── Full job (end to end, fake Shopify) ──────────────────────


@pytest.fixture
def kb_isolated(tmp_path):
    """Point the knowledge_base singleton at a temp DB for the duration of a test."""
    from agent.knowledge_base import knowledge_base

    original = knowledge_base.db_path
    knowledge_base.db_path = str(tmp_path / "kb_job.db")

    async def _init():
        await knowledge_base.init()

    yield knowledge_base, _init
    knowledge_base.db_path = original


async def test_run_sync_incremental_then_prunes(kb_isolated):
    kb, init = kb_isolated
    await init()
    import api.customer_support as cs

    original_shopify = cs._agent.shopify
    try:
        cs._agent.shopify = _FakeShopify()
        await ps.run_sync(force=False)
        assert ps._state["status"] == "idle", ps._state["error"]
        assert ps._state["products_updated"] == 1
        assert ps._state["policies_updated"] == 1
        assert ps._state["chunks_added"] > 0

        await ps.run_sync(force=False)
        assert ps._state["products_updated"] == 0
        assert ps._state["products_skipped"] == 1
        assert ps._state["policies_updated"] == 0

        cs._agent.shopify = _FakeShopify(products=[])  # product deleted from catalog
        await ps.run_sync(force=False)
        sources = await kb.list_sources()
        assert "product:linen-shirt" not in sources
        assert "policy:shipping-policy" in sources
    finally:
        cs._agent.shopify = original_shopify


async def test_run_sync_reports_error_when_disabled(kb_isolated):
    kb, init = kb_isolated
    await init()
    import api.customer_support as cs

    original_shopify = cs._agent.shopify

    class _Disabled(_FakeShopify):
        enabled = False

    try:
        cs._agent.shopify = _Disabled()
        await ps.run_sync(force=False)
        assert ps._state["status"] == "error"
        assert ps._state["error"]
    finally:
        cs._agent.shopify = original_shopify


# ── API endpoints ────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setenv("TENANT_NAME", "test")
    from agent.config import settings

    settings.DB_PATH = str(tmp_path / "test_kb_api.db")
    from agent.storage import store as _store

    _store.db_path = settings.DB_PATH
    settings.REQUIRE_API_KEY = True
    settings.API_KEY = SecretStr("test-key-123")
    settings.RATE_LIMIT_PER_MINUTE = 100

    from agent.knowledge_base import knowledge_base

    original_kb_path = knowledge_base.db_path
    knowledge_base.db_path = str(tmp_path / "kb_api.db")

    import importlib

    import api.customer_support as cs_module
    import api.main as main_module

    importlib.reload(cs_module)
    importlib.reload(main_module)
    cs_module._agent.shopify = _FakeShopify()

    with TestClient(main_module.app, raise_server_exceptions=False) as c:
        yield c, cs_module, knowledge_base
    knowledge_base.db_path = original_kb_path


AUTH = {"X-API-Key": "test-key-123"}


def test_sync_endpoint_starts_and_reports_counters(client):
    c, _, _ = client
    r = c.post("/support/knowledge-base/sync-shopify", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["status"] in ("started", "running")
    status = c.get("/support/knowledge-base/sync-status", headers=AUTH).json()
    assert status["status"] == "idle"  # TestClient waits for background tasks
    assert status["error"] is None
    assert status["products_seen"] == 1
    assert status["products_updated"] == 1


def test_sync_endpoint_requires_api_key(client):
    c, _, _ = client
    assert c.post("/support/knowledge-base/sync-shopify").status_code == 401


def test_sync_endpoint_400_when_shopify_disabled(client):
    c, cs_module, _ = client
    cs_module._agent.shopify = type("Off", (), {"enabled": False})()
    r = c.post("/support/knowledge-base/sync-shopify", headers=AUTH)
    assert r.status_code == 400
    assert r.json()["error"] == "SHOPIFY_NOT_CONFIGURED"


def test_live_stock_endpoint_returns_real_time_snapshot(client):
    c, _, _ = client
    r = c.get("/support/knowledge-base/live-stock?handle=linen-shirt", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["handle"] == "linen-shirt"
    assert body["variants"][0]["status"] == "in_stock"
    assert body["variants"][0]["quantity"] == 12
    assert body["checked_at"]


def test_live_stock_endpoint_404_for_unknown_handle(client):
    c, _, _ = client
    r = c.get("/support/knowledge-base/live-stock?handle=nope", headers=AUTH)
    assert r.status_code == 404
    assert r.json()["error"] == "PRODUCT_NOT_FOUND"
