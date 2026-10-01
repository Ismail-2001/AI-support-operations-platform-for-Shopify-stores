"""
Setup-wizard endpoint tests. Everything runs in-process with temp SQLite and
fake network/LLM components — no real Shopify, Gemini, or .env writes.
"""

import os
import sys
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent.rate_limit as rate_limit_module
from agent import setup_store
from agent.config import settings
from tests.conftest import FakeClassifier, FakeResponseEngine, FakeShopify

AUTH = {"X-API-Key": "test-key-123"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings.DB_PATH = str(tmp_path / "test_setup.db")
    from agent.storage import store as _store

    _store.db_path = settings.DB_PATH
    settings.REQUIRE_API_KEY = True
    settings.API_KEY = SecretStr("test-key-123")
    settings.GOOGLE_API_KEY = SecretStr("dummy-google-key")
    settings.RATE_LIMIT_PER_MINUTE = 100
    rate_limit_module._request_log.clear()

    # Never touch the real .env from a test.
    monkeypatch.setattr(setup_store, "ENV_PATH", tmp_path / ".env")
    # Restore Shopify settings after tests that mutate them.
    monkeypatch.setattr(settings, "SHOPIFY_SHOP_DOMAIN", settings.SHOPIFY_SHOP_DOMAIN)
    monkeypatch.setattr(settings, "SHOPIFY_ACCESS_TOKEN", settings.SHOPIFY_ACCESS_TOKEN)

    import importlib

    import api.customer_support as cs_module
    import api.main as main_module

    importlib.reload(cs_module)
    importlib.reload(main_module)

    cs_module._agent.classifier = FakeClassifier()
    cs_module._agent.response_engine = FakeResponseEngine()
    cs_module._agent.shopify = FakeShopify()
    cs_module._agent._graph = None

    with TestClient(main_module.app, raise_server_exceptions=False) as c:
        yield c, cs_module


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient during Shopify validation."""

    status_code = 200
    payload: ClassVar[dict] = {
        "shop": {"name": "Acme Store", "email": "owner@acme.com", "currency": "USD"}
    }

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, headers=None, params=None):
        return _FakeResp(self.status_code, self.payload)


# ── Status ────────────────────────────────────────────────────────────────────


def test_status_requires_api_key(client):
    c, _ = client
    assert c.get("/support/setup").status_code == 401


def test_status_shape_and_default_steps(client):
    c, _ = client
    r = c.get("/support/setup", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["shopify"]["connected"] is False
    assert body["knowledge_base"]["chunk_count"] == 0
    assert body["voice_set"] is False
    assert body["setup_complete"] is False
    assert body["steps"] == {"shopify": False, "policies": False, "voice": False, "test": False}
    assert body["voice"]["tone"] == "friendly"


# ── Shopify connect ───────────────────────────────────────────────────────────


def test_shopify_connect_success_saves_everywhere(client, monkeypatch):
    c, cs_module = client
    monkeypatch.setattr("api.setup.httpx.AsyncClient", _FakeAsyncClient)

    r = c.post(
        "/support/setup/shopify",
        headers=AUTH,
        json={
            "shop_domain": "https://acme-store.myshopify.com/admin",
            "access_token": "shpat_test123",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connected"] is True
    assert body["domain"] == "acme-store.myshopify.com"
    assert body["shop_name"] == "Acme Store"

    # In-memory settings picked up immediately…
    assert settings.SHOPIFY_SHOP_DOMAIN == "acme-store.myshopify.com"
    assert settings.SHOPIFY_ACCESS_TOKEN.get_secret_value() == "shpat_test123"
    # …the running agent was refreshed…
    assert cs_module._agent.shopify.enabled is True
    # …and .env was written (the temp one, not the real file).
    env_text = setup_store.ENV_PATH.read_text(encoding="utf-8")
    assert "SHOPIFY_SHOP_DOMAIN=acme-store.myshopify.com" in env_text
    assert "SHOPIFY_ACCESS_TOKEN=shpat_test123" in env_text

    status = c.get("/support/setup", headers=AUTH).json()
    assert status["steps"]["shopify"] is True


def test_shopify_connect_rejects_bad_token(client, monkeypatch):
    c, _ = client

    class _Unauthorized(_FakeAsyncClient):
        status_code = 401
        payload: ClassVar[dict] = {"errors": "Unauthorized"}

    monkeypatch.setattr("api.setup.httpx.AsyncClient", _Unauthorized)
    r = c.post(
        "/support/setup/shopify",
        headers=AUTH,
        json={"shop_domain": "acme.myshopify.com", "access_token": "shpat_bad"},
    )
    assert r.status_code == 401
    assert r.json()["error"] == "SHOPIFY_INVALID_CREDENTIALS"
    assert settings.SHOPIFY_SHOP_DOMAIN != "acme.myshopify.com"


def test_shopify_connect_rejects_garbage_domain(client):
    c, _ = client
    r = c.post(
        "/support/setup/shopify",
        headers=AUTH,
        json={"shop_domain": "   ", "access_token": "shpat_x"},
    )
    assert r.status_code == 422


# ── Brand voice ───────────────────────────────────────────────────────────────


def test_voice_roundtrip(client):
    c, _ = client
    r = c.put(
        "/support/setup/voice",
        headers=AUTH,
        json={
            "store_name": "Northwind Supply",
            "tone": "professional",
            "sign_off": "Thanks! — The Northwind team",
            "support_email": "help@northwind.example",
        },
    )
    assert r.status_code == 200, r.text

    status = c.get("/support/setup", headers=AUTH).json()
    assert status["voice_set"] is True
    assert status["voice"]["store_name"] == "Northwind Supply"
    assert status["voice"]["tone"] == "professional"
    assert status["steps"]["voice"] is True


def test_voice_rejects_invalid_tone(client):
    c, _ = client
    r = c.put(
        "/support/setup/voice",
        headers=AUTH,
        json={"store_name": "X", "tone": "sarcastic", "sign_off": "", "support_email": ""},
    )
    assert r.status_code == 422


def test_voice_rejects_bad_email(client):
    c, _ = client
    r = c.put(
        "/support/setup/voice",
        headers=AUTH,
        json={
            "store_name": "X",
            "tone": "friendly",
            "sign_off": "",
            "support_email": "not-an-email",
        },
    )
    assert r.status_code == 422


# ── Test draft (dry run) ──────────────────────────────────────────────────────


def test_test_endpoint_returns_draft_and_persists_nothing(client):
    c, cs_module = client
    before = c.get("/support/tickets", headers=AUTH).json()["total"]

    r = c.post(
        "/support/setup/test",
        headers=AUTH,
        json={"question": "Where is my order #1002? It has been a week."},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["classification"]["category"] == "order_status"
    assert body["suggestion"]["suggested_response"] == "Test response"

    # Nothing landed in the ticket store — dry run really is dry.
    after = c.get("/support/tickets", headers=AUTH).json()["total"]
    assert after == before
    assert not any(
        t["id"].startswith("setup_test_")
        for t in c.get("/support/tickets", headers=AUTH).json()["tickets"]
    )

    # But progress is recorded so the wizard can mark step 4 done.
    status = c.get("/support/setup", headers=AUTH).json()
    assert status["test_done"] is True
    assert status["steps"]["test"] is True


def test_test_endpoint_rejects_too_short_question(client):
    c, _ = client
    r = c.post("/support/setup/test", headers=AUTH, json={"question": "hi"})
    assert r.status_code == 422


# ── Voice block in the prompt (unit) ─────────────────────────────────────────


def test_voice_block_empty_when_defaults():
    from agent.response_engine import build_voice_block

    voice = {"store_name": "", "tone": "friendly", "sign_off": "", "support_email": ""}
    assert build_voice_block(voice) == ""


def test_voice_block_includes_custom_fields():
    from agent.response_engine import build_voice_block

    voice = {
        "store_name": "Acme",
        "tone": "casual",
        "sign_off": "— Acme team",
        "support_email": "hi@acme.co",
    }
    block = build_voice_block(voice)
    assert "Acme" in block
    assert "Tone:" in block
    assert "hi@acme.co" in block
    assert "— Acme team" in block
