"""Tests for tenant isolation guardrails.

Covers:
- TENANT_NAME unset -> Settings() raises ValidationError
- /support/whoami returns correct tenant info
- DB_PATH is auto-prefixed with tenant name when using the default path
- DB_PATH is NOT modified when an explicit path is configured"""

import importlib
import os
import sys

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.config import resolve_db_path
from tests.conftest import FakeClassifier, FakeResponseEngine, FakeShopify


class TestTenantNameRequired:
    def test_settings_raises_without_tenant_name(self):
        """TENANT_NAME is required with no default — pydantic must reject a Settings()
        construction when the env var AND the .env file are absent."""
        from agent.config import Settings

        saved = os.environ.pop("TENANT_NAME", None)
        try:
            with pytest.raises(ValidationError, match="TENANT_NAME"):
                # _env_file=None keeps this test independent of the repo's real
                # .env file, which legitimately sets TENANT_NAME for local runs.
                Settings(_env_file=None)
        finally:
            if saved is not None:
                os.environ["TENANT_NAME"] = saved


class TestDbPathAutoPrefix:
    def test_default_db_is_prefixed_with_tenant(self):
        assert resolve_db_path("cs_agent.db", "acme") == "cs_agent_acme.db"

    def test_empty_tenant_does_not_prefix(self):
        assert resolve_db_path("cs_agent.db", "") == "cs_agent.db"

    def test_custom_db_path_left_unchanged(self):
        assert resolve_db_path("/app/data/custom.db", "acme") == "/app/data/custom.db"

    def test_default_db_with_empty_tenant_returns_default(self):
        assert resolve_db_path("cs_agent.db", "") == "cs_agent.db"

    def test_empty_string_path_with_tenant(self):
        assert resolve_db_path("", "acme") == ""


class TestWhoamiEndpoint:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "dummy-key-for-tests")
        monkeypatch.setenv("TENANT_NAME", "acme-corp")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-dummy")

        from agent.config import settings as s

        s.TENANT_NAME = "acme-corp"
        s.GOOGLE_API_KEY = SecretStr("dummy-key-for-tests")
        s.DB_PATH = str(tmp_path / "test_whoami.db")
        s.REQUIRE_API_KEY = True
        s.API_KEY = SecretStr("whoami-test-key")
        s.SHOPIFY_SHOP_DOMAIN = "acme.myshopify.com"
        s.GORGIAS_DOMAIN = "acme"

        import api.customer_support as cs_module
        import api.main as main_module

        importlib.reload(cs_module)
        importlib.reload(main_module)

        cs_module._agent.classifier = FakeClassifier()
        cs_module._agent.response_engine = FakeResponseEngine()
        cs_module._agent.shopify = FakeShopify()

        with TestClient(main_module.app, raise_server_exceptions=False) as c:
            yield c, cs_module, s

    def test_whoami_requires_api_key(self, client):
        c, _, _ = client
        r = c.get("/support/whoami")
        assert r.status_code == 401

    def test_whoami_returns_tenant_info(self, client):
        c, _, _ = client
        r = c.get("/support/whoami", headers={"X-API-Key": "whoami-test-key"})
        assert r.status_code == 200
        data = r.json()
        assert data["tenant_name"] == "acme-corp"
        assert data["shopify_domain"] == "acme.myshopify.com"
        assert data["gorgias_domain"] == "acme"

    def test_whoami_tenant_name_matches_config(self, client):
        c, _, s = client
        r = c.get("/support/whoami", headers={"X-API-Key": "whoami-test-key"})
        assert r.json()["tenant_name"] == s.TENANT_NAME
