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
