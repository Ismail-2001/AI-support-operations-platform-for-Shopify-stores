"""Tests for tenacity-based retry logic in shopify and LLM paths.
Predicates are tested directly; invoke_with_fallback is exercised with
mocked chains to verify retry-vs-fallthrough behaviour."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from agent.llm import _is_transient_llm_error, invoke_with_fallback
from integrations.shopify import _is_timeout_or_connection_error, _is_transient_shopify_error

# ── Predicate unit tests ────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


def _http_error(status: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(str(status), request=MagicMock(), response=_FakeResponse(status))


class TestLlmRetryPredicate:
    def test_timeout_is_retryable(self):
        assert _is_transient_llm_error(httpx.TimeoutException("timeout")) is True

    def test_transport_error_is_retryable(self):
        assert _is_transient_llm_error(httpx.TransportError("connection reset")) is True

    def test_5xx_is_retryable(self):
        assert _is_transient_llm_error(_http_error(503)) is True
        assert _is_transient_llm_error(_http_error(500)) is True
        assert _is_transient_llm_error(_http_error(502)) is True

    def test_4xx_is_not_retryable(self):
        assert _is_transient_llm_error(_http_error(400)) is False
        assert _is_transient_llm_error(_http_error(401)) is False
        assert _is_transient_llm_error(_http_error(403)) is False
        assert _is_transient_llm_error(_http_error(404)) is False
        assert _is_transient_llm_error(_http_error(422)) is False

    def test_429_is_retryable(self):
        assert _is_transient_llm_error(_http_error(429)) is True

    def test_value_error_is_not_retryable(self):
        assert _is_transient_llm_error(ValueError("bad")) is False


class TestShopifyRetryPredicate:
    def test_timeout_is_retryable(self):
        assert _is_transient_shopify_error(httpx.TimeoutException("timeout")) is True

    def test_transport_error_is_retryable(self):
        assert _is_transient_shopify_error(httpx.TransportError("connection reset")) is True

    def test_5xx_is_retryable(self):
        assert _is_transient_shopify_error(_http_error(503)) is True

    def test_4xx_is_not_retryable(self):
        assert _is_transient_shopify_error(_http_error(400)) is False
        assert _is_transient_shopify_error(_http_error(422)) is False

    def test_value_error_is_not_retryable(self):
        assert _is_transient_shopify_error(ValueError("bad")) is False


class TestShopifyRefundPredicate:
    """create_refund uses _is_timeout_or_connection_error — even 5xx is *not* retried."""

    def test_timeout_is_retryable(self):
        assert _is_timeout_or_connection_error(httpx.TimeoutException("timeout")) is True

    def test_transport_error_is_retryable(self):
        assert _is_timeout_or_connection_error(httpx.TransportError("connection reset")) is True

    def test_5xx_is_not_retryable(self):
        assert _is_timeout_or_connection_error(_http_error(503)) is False

    def test_4xx_is_not_retryable(self):
        assert _is_timeout_or_connection_error(_http_error(422)) is False
        assert _is_timeout_or_connection_error(_http_error(400)) is False

    def test_value_error_is_not_retryable(self):
        assert _is_timeout_or_connection_error(ValueError("bad")) is False


# ── invoke_with_fallback retry behaviour ────────────────────


async def test_retry_transient_timeout_eventually_succeeds():
    """Fails twice with timeout then succeeds -> 3 attempts, returns primary result."""
    primary = AsyncMock()
    primary.ainvoke = AsyncMock(
        side_effect=[
            httpx.TimeoutException("timeout 1"),
            httpx.TimeoutException("timeout 2"),
            {"parsed": "ok", "raw": None},
        ]
    )
    fallback = AsyncMock()

    result, model = await invoke_with_fallback(
        primary,
        fallback,
        ["msg"],
        primary_model_name="gemini-2.0-flash",
        fallback_model_name="claude-haiku",
    )

    assert result["parsed"] == "ok"
    assert model == "gemini-2.0-flash"
    assert primary.ainvoke.await_count == 3
    fallback.ainvoke.assert_not_awaited()


async def test_retry_client_400_error_does_not_retry():
    """4xx client error -> no retry, fails immediately."""
    primary = AsyncMock()
    primary.ainvoke = AsyncMock(side_effect=_http_error(400))

    with pytest.raises(httpx.HTTPStatusError):
        await invoke_with_fallback(
            primary,
            None,
            ["msg"],
            primary_model_name="gemini-2.0-flash",
            fallback_model_name="claude-haiku",
        )

    assert primary.ainvoke.await_count == 1


async def test_retry_all_attempts_exhausted_raises_original():
    """All 3 primary retries fail, fallback also fails -> original primary error raised."""
    primary = AsyncMock()
    primary.ainvoke = AsyncMock(side_effect=httpx.TimeoutException("upstream down"))
    fallback = AsyncMock()
    fallback.ainvoke = AsyncMock(side_effect=httpx.TimeoutException("fallback also down"))

    with pytest.raises(httpx.TimeoutException, match="upstream down"):
        await invoke_with_fallback(
            primary,
            fallback,
            ["msg"],
            primary_model_name="gemini-2.0-flash",
            fallback_model_name="claude-haiku",
        )

    assert primary.ainvoke.await_count == 3
    assert fallback.ainvoke.await_count == 1


# ── Shopify create_refund is never retried on non-timeout rejection ──


async def test_shopify_refund_422_not_retried():
    """Simulate a Shopify refund rejection (422) -> retry predicate rejects it,
    function is called exactly once using the same retry config as create_refund."""
    from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

    _test_retry = retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception(_is_timeout_or_connection_error),
    )

    call_count = [0]

    @_test_retry
    async def fake_refund():
        call_count[0] += 1
        raise _http_error(422)

    with pytest.raises(httpx.HTTPStatusError):
        await fake_refund()

    assert call_count[0] == 1, "create_refund must not retry on a 422 rejection"
