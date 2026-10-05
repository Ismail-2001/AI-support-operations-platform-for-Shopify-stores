"""Multi-store / agency packaging tests.

Registry CRUD (global, ignores X-Store-Id), X-Store-Id routing through the
middleware, data isolation between stores' DB files, per-store agent
credentials, and ContextProxy semantics.
"""

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agent import multistore
from agent.config import settings
from agent.context_proxy import ContextProxy
from agent.storage import TicketStore
from tests.conftest import FakeClassifier, FakeResponseEngine, FakeShopify

AUTH = {"X-API-Key": "test-key-123"}


@pytest.fixture(autouse=True)
def _clean_multistore_caches():
    multistore.reset_caches()
    yield
    multistore.reset_caches()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-key-for-tests")
    monkeypatch.setenv("TENANT_NAME", "test")
    settings.GOOGLE_API_KEY = SecretStr("dummy-key-for-tests")
    settings.DB_PATH = str(tmp_path / "test_multistore.db")
    from agent.storage import store as _store

    _store.db_path = settings.DB_PATH
    settings.REQUIRE_API_KEY = True
    settings.API_KEY = SecretStr("test-key-123")
    settings.RATE_LIMIT_PER_MINUTE = 100
    settings.ACTION_RATE_LIMIT_PER_MINUTE = 100

    import agent.rate_limit as rate_limit_module

    rate_limit_module._request_log.clear()

    import agent.storage as storage_module
    import agent.support_agent as sa_module
    import api.customer_support as cs_module
    import api.main as main_module

    # Rebuild the storage proxy before reloading so this test is immune to any
    # test elsewhere swapping `store` for a raw TicketStore (context routing
    # only works through the proxy).
    storage_module.store = ContextProxy(
        TicketStore(), lambda: multistore.ticket_store_for_current()
    )
    sa_module.store = storage_module.store

    importlib.reload(cs_module)
    importlib.reload(main_module)

    cs_module._agent.classifier = FakeClassifier()
    cs_module._agent.response_engine = FakeResponseEngine()
    cs_module._agent.shopify = FakeShopify()

    with TestClient(main_module.app, raise_server_exceptions=False) as c:
        yield c, cs_module


def _create_store(c, name, domain="", token=None):
    payload: dict = {"name": name}
    if domain:
        payload["shop_domain"] = domain
    if token:
        payload["shopify_access_token"] = token
    r = c.post("/support/stores", headers=AUTH, json=payload)
    assert r.status_code == 201, r.text
    return r.json()["store"]


def _ticket_payload(tag):
    return {
        "customer_email": f"{tag}@example.test",
        "subject": "Where is my order?",
        "body": f"Hello, this is {tag} asking about my order.",
    }


# --- Registry CRUD ---


def test_registry_crud_and_credential_redaction(client):
    c, _cs = client
    rec = _create_store(c, "Acme", "acme.myshopify.com", "shpat_supersecret")
    assert rec["has_shopify_token"] is True
    assert "shpat_supersecret" not in str(rec)
    sid = rec["id"]

    listed = c.get("/support/stores", headers=AUTH)
    assert listed.status_code == 200
    assert [s["id"] for s in listed.json()["stores"]] == [sid]
    assert "shpat_supersecret" not in listed.text

    one = c.get(f"/support/stores/{sid}", headers=AUTH)
    assert one.status_code == 200
    assert one.json()["store"]["name"] == "Acme"

    patched = c.patch(
        f"/support/stores/{sid}",
        headers=AUTH,
        json={"name": "Acme Inc", "shopify_access_token": ""},
    )
    assert patched.status_code == 200
    body = patched.json()["store"]
    assert body["name"] == "Acme Inc"
    assert body["has_shopify_token"] is False

    assert c.get("/support/stores/does-not-exist", headers=AUTH).status_code == 404

    deleted = c.delete(f"/support/stores/{sid}", headers=AUTH)
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "store_id": sid}
    assert c.get(f"/support/stores/{sid}", headers=AUTH).status_code == 404
    assert c.get("/support/stores", headers=AUTH).json()["stores"] == []


def test_registry_rejects_duplicate_shop_domain(client):
    c, _cs = client
    _create_store(c, "Acme", "acme.myshopify.com")
    r = c.post(
        "/support/stores",
        headers=AUTH,
        json={"name": "Copycat", "shop_domain": "ACME.myshopify.com"},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "STORE_ALREADY_EXISTS"

    # Empty domains never collide (they are "not connected yet" stores).
    _create_store(c, "Store A", "")
    _create_store(c, "Store B", "")


def test_registry_endpoints_ignore_store_context(client):
    c, _cs = client
    a = _create_store(c, "A", "a.myshopify.com")
    b = _create_store(c, "B", "b.myshopify.com")

    r = c.get("/support/stores", headers={**AUTH, "X-Store-Id": a["id"]})
    assert r.status_code == 200
    assert {s["id"] for s in r.json()["stores"]} == {a["id"], b["id"]}

    # Creating from a store-scoped request still lands in the global registry.
    made = c.post(
        "/support/stores",
        headers={**AUTH, "X-Store-Id": a["id"]},
        json={"name": "C"},
    )
    assert made.status_code == 201
    all_stores = c.get("/support/stores", headers=AUTH).json()["stores"]
    assert len(all_stores) == 3


# --- X-Store-Id routing ---


def test_unknown_store_id_returns_404(client):
    c, _cs = client
    r = c.get("/support/tickets", headers={**AUTH, "X-Store-Id": "missing-store"})
    assert r.status_code == 404
    assert r.json()["error"] == "STORE_NOT_FOUND"


def test_store_header_routes_writes_and_reads_to_isolated_db(client):
    c, cs = client
    rec = _create_store(c, "Acme", "acme.myshopify.com", "shpat_x")
    sid = rec["id"]
    hdrs = {**AUTH, "X-Store-Id": sid}

    # First request builds the per-store context (middleware) — swap the
    # store's agent for fakes so the pipeline makes no LLM/API calls.
    warm = c.get("/support/tickets", headers=hdrs)
    assert warm.status_code == 200
    assert sid in multistore._agents
    store_agent = multistore._agents[sid]
    store_agent.classifier = FakeClassifier()
    store_agent.response_engine = FakeResponseEngine()
    store_agent.shopify = FakeShopify()

    # Also fake the deployment-default agent for the no-header write.
    cs._agent.classifier = FakeClassifier()
    cs._agent.response_engine = FakeResponseEngine()
    cs._agent.shopify = FakeShopify()

    created_store = c.post("/support/tickets", headers=hdrs, json=_ticket_payload("store-customer"))
    assert created_store.status_code == 200, created_store.text
    store_ticket_id = created_store.json()["ticket_id"]

    created_default = c.post(
        "/support/tickets", headers=AUTH, json=_ticket_payload("default-customer")
    )
    assert created_default.status_code == 200, created_default.text
    default_ticket_id = created_default.json()["ticket_id"]

    # Physical isolation: each write landed in exactly the right file.
    def ticket_ids_in(db_path):
        import sqlite3

        con = sqlite3.connect(db_path)
        try:
            return {row[0] for row in con.execute("SELECT id FROM tickets")}
        finally:
            con.close()

    store_file_ids = ticket_ids_in(multistore.store_db_path(sid))
    primary_ids = ticket_ids_in(settings.DB_PATH)
    assert (
        store_ticket_id in store_file_ids
    ), f"store ticket missing from {multistore.store_db_path(sid)}: {store_file_ids}"
    assert (
        default_ticket_id in primary_ids
    ), f"default ticket missing from primary DB: {primary_ids}"
    assert (
        default_ticket_id not in store_file_ids
    ), f"default ticket leaked into store DB: {store_file_ids}"
    assert store_ticket_id not in primary_ids, f"store ticket leaked into primary DB: {primary_ids}"

    store_view = c.get("/support/tickets", headers=hdrs).json()["tickets"]
    store_ids = [t["id"] for t in store_view]
    assert store_ticket_id in store_ids
    assert default_ticket_id not in store_ids

    default_view = c.get("/support/tickets", headers=AUTH).json()["tickets"]
    default_ids = [t["id"] for t in default_view]
    assert default_ticket_id in default_ids
    assert store_ticket_id not in default_ids


def test_per_store_agent_uses_store_credentials(client):
    c, _cs = client
    rec = _create_store(c, "Acme", "acme.myshopify.com", "shpat_secret")
    sid = rec["id"]

    r = c.get("/support/tickets", headers={**AUTH, "X-Store-Id": sid})
    assert r.status_code == 200
    agent = multistore._agents[sid]
    assert agent.shopify.enabled is True
    assert agent.shopify.shop_domain == "acme.myshopify.com"


def test_update_store_invalidates_cached_context(client):
    c, _cs = client
    rec = _create_store(c, "Acme", "acme.myshopify.com", "shpat_v1")
    sid = rec["id"]
    hdrs = {**AUTH, "X-Store-Id": sid}

    assert c.get("/support/tickets", headers=hdrs).status_code == 200
    assert sid in multistore._ticket_stores

    patched = c.patch(f"/support/stores/{sid}", headers=AUTH, json={"name": "Acme v2"})
    assert patched.status_code == 200
    assert sid not in multistore._ticket_stores
    assert sid not in multistore._agents

    assert c.get("/support/tickets", headers=hdrs).status_code == 200
    assert sid in multistore._ticket_stores
    assert multistore._store_records[sid]["name"] == "Acme v2"


def test_delete_store_drops_caches_and_header_becomes_404(client):
    c, _cs = client
    rec = _create_store(c, "Acme", "acme.myshopify.com")
    sid = rec["id"]
    hdrs = {**AUTH, "X-Store-Id": sid}

    assert c.get("/support/tickets", headers=hdrs).status_code == 200
    assert sid in multistore._ticket_stores
    # Fake the store's agent before writing (same reason as the isolation
    # test above: the pipeline must not make real LLM calls here).
    store_agent = multistore._agents[sid]
    store_agent.classifier = FakeClassifier()
    store_agent.response_engine = FakeResponseEngine()
    store_agent.shopify = FakeShopify()

    # Writes a ticket first so we can prove the data file survives deletion.
    assert c.post("/support/tickets", headers=hdrs, json=_ticket_payload("bye")).status_code == 200
    data_file = Path(multistore.store_db_path(sid))
    assert data_file.exists()

    assert c.delete(f"/support/stores/{sid}", headers=AUTH).status_code == 200
    assert sid not in multistore._ticket_stores
    assert sid not in multistore._agents

    # No silent data destruction: file stays for re-attach/ops recovery.
    assert data_file.exists()

    assert c.get("/support/tickets", headers=hdrs).status_code == 404


# --- ContextProxy unit behavior ---


class _Thing:
    def __init__(self, x=1):
        self.x = x


def test_proxy_forwards_to_default_without_context():
    default = _Thing(1)
    proxy = ContextProxy(default, lambda: None)
    assert proxy.x == 1
    proxy.x = 2
    assert default.x == 2
    assert "ContextProxy" in repr(proxy)


def test_proxy_forwards_to_resolved_target():
    default = _Thing(1)
    other = _Thing(9)
    current = {"target": None}
    proxy = ContextProxy(default, lambda: current["target"])

    current["target"] = other
    assert proxy.x == 9
    proxy.x = 10
    assert other.x == 10
    assert default.x == 1

    current["target"] = None
    assert proxy.x == 1


def test_resolver_fails_loud_when_context_set_but_not_ready():
    token = multistore.current_store_id.set("ghost-store")
    try:
        with pytest.raises(RuntimeError, match="not initialized"):
            multistore.ticket_store_for_current()
    finally:
        multistore.current_store_id.reset(token)
    # Outside the context everything falls back to None -> default object.
    assert multistore.ticket_store_for_current() is None


def test_store_db_path_is_sibling_of_primary_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "cs_agent_test.db"))
    per_store = Path(multistore.store_db_path("abc123"))
    assert per_store.parent == tmp_path
    assert per_store.name == "cs_store_abc123.db"


# --- Per-store integration settings (return labels / subscriptions) ---

_SETTINGS_PAYLOAD = {
    "shipengine_api_key": "se_secret_value",
    "recharge_api_token": "rc_secret_value",
    "skio_api_token": "sk_secret_value",
    "subscription_provider": "recharge",
    "return_address": {
        "name": "Returns Desk",
        "address1": "1 Main St",
        "city": "Austin",
        "state": "TX",
        "zip": "78701",
        "country": "US",
    },
    "return_window_days": 14,
}


def test_store_integration_settings_create_redaction_and_clear(client):
    c, _cs = client
    r = c.post("/support/stores", headers=AUTH, json={"name": "Agency", **_SETTINGS_PAYLOAD})
    assert r.status_code == 201, r.text
    store = r.json()["store"]
    assert store["has_shipengine_token"] is True
    assert store["has_recharge_token"] is True
    assert store["has_skio_token"] is True
    assert store["subscription_provider"] == "recharge"
    assert store["return_window_days"] == 14
    assert store["return_address"]["city"] == "Austin"
    # Secrets are never echoed, in any registry view.
    for secret in ("se_secret_value", "rc_secret_value", "sk_secret_value"):
        assert secret not in r.text
        assert secret not in c.get("/support/stores", headers=AUTH).text

    sid = store["id"]
    cleared = c.patch(
        f"/support/stores/{sid}",
        headers=AUTH,
        json={
            "shipengine_api_key": "",
            "recharge_api_token": "",
            "return_window_days": 0,
            "return_address": {},
        },
    )
    assert cleared.status_code == 200, cleared.text
    body = cleared.json()["store"]
    assert body["has_shipengine_token"] is False
    assert body["has_recharge_token"] is False
    assert "return_window_days" not in body
    assert "return_address" not in body
    # Untouched fields survive the patch.
    assert body["has_skio_token"] is True


def test_store_integration_settings_validation(client):
    c, _cs = client
    r = c.post(
        "/support/stores",
        headers=AUTH,
        json={"name": "Bad", "subscription_provider": "bold"},
    )
    assert r.status_code == 422

    r2 = c.post(
        "/support/stores",
        headers=AUTH,
        json={"name": "Bad2", "return_address": {"name": "X", "city": "Y"}},
    )
    assert r2.status_code == 422
    assert "missing required fields" in r2.text

    r3 = c.post(
        "/support/stores",
        headers=AUTH,
        json={"name": "Bad3", "return_window_days": 999},
    )
    assert r3.status_code == 422


def test_integration_overrides_none_outside_store_scope():
    from agent.multistore import integration_overrides

    assert integration_overrides() is None

    multistore._store_records["ov1"] = {"credentials": {"return_window_days": 5}}
    token = multistore.current_store_id.set("ov1")
    try:
        assert integration_overrides() == {"return_window_days": 5}
    finally:
        multistore.current_store_id.reset(token)
        multistore._store_records.pop("ov1", None)
    assert integration_overrides() is None


def test_shipengine_client_store_scope_has_no_env_fallback(monkeypatch):
    from integrations.shipengine import ShipEngineClient

    monkeypatch.setattr(settings, "SHIPENGINE_API_KEY", SecretStr("env-key"))
    monkeypatch.setattr(settings, "RETURN_ADDRESS_NAME", "Env Desk")
    monkeypatch.setattr(settings, "RETURN_ADDRESS1", "2 Env Way")
    monkeypatch.setattr(settings, "RETURN_CITY", "Denver")
    monkeypatch.setattr(settings, "RETURN_ZIP", "80201")

    # Outside store scope: env config applies.
    outside = ShipEngineClient()
    assert outside.enabled is True
    assert outside.configured == (True, [])

    # Inside a store scope with no ShipEngine key: DISABLED despite the env key
    # (one tenant's carrier account must never serve another tenant).
    multistore._store_records["se1"] = {"credentials": {}}
    token = multistore.current_store_id.set("se1")
    try:
        scoped = ShipEngineClient()
        assert scoped.enabled is False
        assert scoped.configured == (False, ["SHIPENGINE_API_KEY"])
    finally:
        multistore.current_store_id.reset(token)
        multistore._store_records.pop("se1", None)

    # Store-provided key + address wins inside its scope.
    multistore._store_records["se2"] = {
        "credentials": {
            "shipengine_api_key": "store-key",
            "return_address": {
                "name": "Store Returns",
                "address1": "9 Store Rd",
                "city": "Miami",
                "zip": "33101",
                "country": "US",
            },
        }
    }
    token = multistore.current_store_id.set("se2")
    try:
        scoped = ShipEngineClient()
        assert scoped.enabled is True
        assert scoped.configured == (True, [])
        assert scoped._api_key() == "store-key"
        assert scoped._address_field("address1") == "9 Store Rd"
    finally:
        multistore.current_store_id.reset(token)
        multistore._store_records.pop("se2", None)


def test_per_store_return_window_applies_through_endpoint(client):
    """Full chain: store settings -> X-Store-Id request -> eligibility window."""
    from datetime import UTC, datetime, timedelta

    from tests.conftest import FakeResponseEngine

    c, _cs = client
    r = c.post(
        "/support/stores",
        headers=AUTH,
        json={"name": "Short Window", "return_window_days": 3},
    )
    assert r.status_code == 201, r.text
    sid = r.json()["store"]["id"]
    hdrs = {**AUTH, "X-Store-Id": sid}

    # Warm the per-store context, then fake its agent (no LLM calls).
    assert c.get("/support/tickets", headers=hdrs).status_code == 200
    store_agent = multistore._agents[sid]
    store_agent.classifier = FakeClassifier()
    store_agent.response_engine = FakeResponseEngine()

    class _OrderShopify:
        enabled = True
        order = None

        async def get_order_by_id(self, order_id):
            return self.order

        async def get_order_by_number(self, order_number):
            return self.order

        async def summarize_order(self, order):
            return "order summary"

    store_agent.shopify = _OrderShopify()
    store_agent.shopify.order = {
        "id": "ord-win",
        "created_at": (datetime.now(UTC) - timedelta(days=5)).isoformat(),
        "financial_status": "paid",
        "fulfillment_status": "fulfilled",
        "line_items": [{"id": 1, "title": "Mug", "quantity": 1}],
        "shipping_address": {
            "first_name": "A",
            "last_name": "B",
            "address1": "1 Customer St",
            "city": "Portland",
            "zip": "97201",
            "country": "US",
        },
    }

    created = c.post("/support/tickets", headers=hdrs, json=_ticket_payload("returner"))
    assert created.status_code == 200, created.text
    ticket_id = created.json()["ticket_id"]

    ts = TicketStore(db_path=multistore.store_db_path(sid))
    import asyncio

    asyncio.run(ts.update_status(ticket_id, order_id="ord-win"))

    # 5-day-old order vs the store's 3-day window -> expired, store window shown.
    elig = c.get(f"/support/tickets/{ticket_id}/return-eligibility", headers=hdrs)
    assert elig.status_code == 200, elig.text
    body = elig.json()
    assert body["window_days"] == 3
    assert body["eligible"] is False
    assert "expired" in body["reason"]

    # The same order under the deployment default (30 days) would be eligible.
    default = c.get(f"/support/tickets/{ticket_id}/return-eligibility", headers=AUTH)
    assert default.status_code == 404  # ticket lives in the store's DB, not primary
