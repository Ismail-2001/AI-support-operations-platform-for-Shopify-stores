"""Circuit-breaker + retry-hardening tests (WP3).

Unit tests for CircuitBreaker/guarded state machines (fake clock), transient
predicates including 429 across all five integration clients, reraise=True
(original exception type after exhaustion), health-endpoint surfacing, and the
CircuitOpenError -> 503 CIRCUIT_OPEN mapping through a real HTTP route.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agent.resilience import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    CircuitBreaker,
    CircuitOpenError,
    circuit_snapshots,
    guarded,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def breaker():
    return CircuitBreaker("test", failure_threshold=3, reset_timeout=30.0, clock=FakeClock())


def test_breaker_opens_after_threshold_and_blocks(breaker):
    for _ in range(3):
        breaker.before_call()
        breaker.record_failure()
    assert breaker.state == OPEN
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
    # Open breaker rejects without consuming the reset window.
    assert breaker.snapshot()["failures"] == 3


def test_breaker_half_open_trial_success_closes(breaker):
    for _ in range(3):
        breaker.before_call()
        breaker.record_failure()
    breaker._clock.now = 31.0  # past reset_timeout
    breaker.before_call()  # flips to half-open, lets the trial through
    assert breaker.state == HALF_OPEN
    # Concurrent callers are still rejected while the trial is in flight.
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
    breaker.record_success()
    assert breaker.state == CLOSED
    assert breaker.snapshot()["failures"] == 0


def test_breaker_half_open_trial_failure_reopens(breaker):
    for _ in range(3):
        breaker.before_call()
        breaker.record_failure()
    breaker._clock.now = 31.0
    breaker.before_call()
    breaker.record_failure()  # trial failed -> reopen
    assert breaker.state == OPEN
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
    # Full timeout must apply again after the failed trial.
    breaker._clock.now = 50.0  # 19s after reopen - still open
    with pytest.raises(CircuitOpenError):
        breaker.before_call()


def test_breaker_neutral_outcome_releases_trial_without_changing_state(breaker):
    for _ in range(3):
        breaker.before_call()
        breaker.record_failure()
    breaker._clock.now = 31.0
    breaker.before_call()
    breaker.record_neutral()  # e.g. our own validation error during the trial
    assert breaker.state == HALF_OPEN
    breaker._clock.now = 62.0
    breaker.before_call()  # a later call can take another trial (not wedged)
    breaker.record_success()
    assert breaker.state == CLOSED


def test_breaker_on_open_hook_fires_once():
    clock = FakeClock()
    b = CircuitBreaker("hooked", failure_threshold=2, reset_timeout=30.0, clock=clock)
    opened = []
    b.on_open = opened.append
    b.before_call()
    b.record_failure()
    assert opened == []
    b.before_call()
    b.record_failure()  # threshold -> open, hook fires
    assert opened == ["hooked"]
    with pytest.raises(CircuitOpenError):
        b.before_call()
    assert opened == ["hooked"]  # blocked calls do not re-fire


async def _ok():
    return "fine"


async def _boom():
    raise httpx.ConnectError("down")


async def _neutral():
    raise ValueError("our own bug")


def test_guarded_records_failures_and_short_circuits(breaker):
    is_failure = lambda e: True  # noqa: E731
    guarded_ok = guarded(breaker, is_failure)(_ok)
    guarded_boom = guarded(breaker, is_failure)(_boom)

    # Success path records a success and leaves the circuit closed.
    asyncio.run(guarded_ok())
    assert breaker.state == CLOSED
    assert breaker.snapshot()["failures"] == 0

    for _ in range(3):
        with pytest.raises(httpx.ConnectError):
            asyncio.run(guarded_boom())
    assert breaker.state == OPEN

    # Short-circuit: the wrapped coroutine is never entered again.
    async def should_not_run():
        raise AssertionError("circuit open must prevent the call")

    guarded_blocked = guarded(breaker, is_failure)(should_not_run)
    with pytest.raises(CircuitOpenError):
        asyncio.run(guarded_blocked())


def test_guarded_neutral_exceptions_do_not_open(breaker):
    guarded_neutral = guarded(breaker, lambda e: isinstance(e, httpx.ConnectError))(_neutral)
    for _ in range(5):
        with pytest.raises(ValueError):
            asyncio.run(guarded_neutral())
    assert breaker.state == CLOSED
    assert breaker.snapshot()["failures"] == 0


# ── Transient predicates: 429 is retryable everywhere ──────────


def _status_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://example.test")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError("boom", request=req, response=resp)


@pytest.mark.parametrize(
    "predicate_module,predicate_name",
    [
        ("integrations.shopify", "_is_transient_shopify_error"),
        ("integrations.gorgias", "_is_transient_gorgias_error"),
        ("integrations.recharge", "_is_transient"),
        ("integrations.skio", "_is_transient"),
        ("integrations.shipengine", "_is_transient"),
    ],
)
def test_transient_predicates_treat_429_as_retryable(predicate_module, predicate_name):
    import importlib

    module = importlib.import_module(predicate_module)
    predicate = getattr(module, predicate_name)
    assert predicate(_status_error(429)) is True
    assert predicate(_status_error(503)) is True
    assert predicate(_status_error(400)) is False
    assert predicate(_status_error(404)) is False
    assert predicate(httpx.ConnectError("x")) is True


def test_backoff_dicts_all_set_reraise():
    import importlib

    for mod_name in (
        "integrations.shopify",
        "integrations.gorgias",
        "integrations.recharge",
        "integrations.skio",
        "integrations.shipengine",
    ):
        module = importlib.import_module(mod_name)
        assert module._EXP_BACKOFF.get("reraise") is True, mod_name


@pytest.mark.asyncio
async def test_retry_exhaustion_raises_original_exception_type():
    """reraise=True: after 3 failed attempts the caller sees the ORIGINAL
    httpx error, not tenacity's RetryError - so error mapping stays correct.
    The breaker counts LOGICAL calls (one failure per exhausted call)."""
    from integrations.gorgias import GORGIAS_BREAKER, GorgiasClient

    with patch("integrations.gorgias.settings") as mock_settings:
        mock_settings.GORGIAS_DOMAIN = "test-store"
        mock_settings.GORGIAS_EMAIL = "t@g.com"
        mock_settings.GORGIAS_API_KEY = MagicMock(get_secret_value=lambda: "k")
        client = GorgiasClient()

        call_count = 0

        async def always_503(url, **kwargs):
            nonlocal call_count
            call_count += 1
            raise _status_error(503)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(side_effect=always_503)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_http

            GORGIAS_BREAKER.reset()
            with pytest.raises(httpx.HTTPStatusError):
                await client.list_open_tickets()
            assert call_count == 3  # retried twice, third attempt raised
            assert GORGIAS_BREAKER.snapshot()["failures"] == 1
            GORGIAS_BREAKER.reset()


# ── HTTP surface: health + 503 mapping ─────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-key-for-tests")
    monkeypatch.setenv("TENANT_NAME", "test")
    from agent.config import settings

    settings.GOOGLE_API_KEY = SecretStr("dummy-key-for-tests")
    settings.DB_PATH = str(tmp_path / "test_resilience.db")
    # Point the module-level store singleton at the temp file (same pattern as
    # test_api_security: mutate db_path, no proxy swap - these tests are
    # single-store).
    from agent.storage import store as _store

    _store.db_path = settings.DB_PATH
    settings.REQUIRE_API_KEY = True
    settings.API_KEY = SecretStr("test-key-123")
    settings.RATE_LIMIT_PER_MINUTE = 100

    import agent.rate_limit as rate_limit_module

    rate_limit_module._request_log.clear()

    import api.customer_support as cs_module
    import api.main as main_module
    from tests.conftest import FakeClassifier, FakeResponseEngine, FakeShopify

    importlib.reload(cs_module)
    importlib.reload(main_module)
    cs_module._agent.classifier = FakeClassifier()
    cs_module._agent.response_engine = FakeResponseEngine()
    cs_module._agent.shopify = FakeShopify()

    from integrations.gorgias import GORGIAS_BREAKER

    GORGIAS_BREAKER.reset()
    with TestClient(main_module.app, raise_server_exceptions=False) as c:
        yield c, cs_module
    GORGIAS_BREAKER.reset()


AUTH = {"X-API-Key": "test-key-123"}


def test_health_endpoints_expose_circuit_states(client):
    c, _cs = client
    checks = c.get("/health").json()["checks"]
    assert "circuits" in checks
    assert checks["circuits"]["shopify"]["state"] == CLOSED

    support = c.get("/support/health").json()
    assert support["circuits"]["gorgias"]["state"] == CLOSED
    # All five breakers registered and serializable.
    assert set(support["circuits"]) == {"shopify", "gorgias", "recharge", "skio", "shipengine"}
    assert circuit_snapshots().keys() == support["circuits"].keys()


def test_open_circuit_on_gorgias_maps_to_503(client):
    """End-to-end: breaker open -> post_reply short-circuits -> 503 CIRCUIT_OPEN
    from the respond endpoint (not a hang, not a generic 500)."""
    c, _cs = client
    from integrations.gorgias import GORGIAS_BREAKER

    created = c.post(
        "/support/tickets",
        headers=AUTH,
        json={
            "customer_email": "buyer@example.test",
            "subject": "Order question",
            "body": "Where is my order 1042?",
            "metadata": {"gorgias_ticket_id": "g123"},
        },
    )
    assert created.status_code == 200, created.text
    ticket_id = created.json()["ticket_id"]

    for _ in range(GORGIAS_BREAKER.failure_threshold):
        GORGIAS_BREAKER.before_call()
        GORGIAS_BREAKER.record_failure()
    assert GORGIAS_BREAKER.state == OPEN

    resp = c.post(
        f"/support/tickets/{ticket_id}/respond",
        headers=AUTH,
        json={"response": "Checking now!", "send_via_gorgias": True},
    )
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["error"] == "CIRCUIT_OPEN"
    assert body["circuit"] == "gorgias"

    GORGIAS_BREAKER.reset()
