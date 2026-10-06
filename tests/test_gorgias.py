"""Tests for integrations/gorgias.py — retry predicates, webhook normalization,
GorgiasNotConfigured guard, and retry behaviour on transient/permanent errors."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from integrations.gorgias import (
    GorgiasClient,
    GorgiasNotConfigured,
    _is_transient_gorgias_error,
)

# ── Predicate unit tests ────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


def _http_error(status: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(str(status), request=MagicMock(), response=_FakeResponse(status))


class TestGorgiasRetryPredicate:
    def test_timeout_is_retryable(self):
        assert _is_transient_gorgias_error(httpx.TimeoutException("timeout")) is True

    def test_transport_error_is_retryable(self):
        assert _is_transient_gorgias_error(httpx.TransportError("connection reset")) is True

    def test_5xx_is_retryable(self):
        assert _is_transient_gorgias_error(_http_error(500)) is True
        assert _is_transient_gorgias_error(_http_error(502)) is True
        assert _is_transient_gorgias_error(_http_error(503)) is True
        assert _is_transient_gorgias_error(_http_error(504)) is True

    def test_4xx_is_not_retryable(self):
        assert _is_transient_gorgias_error(_http_error(400)) is False
        assert _is_transient_gorgias_error(_http_error(401)) is False
        assert _is_transient_gorgias_error(_http_error(403)) is False
        assert _is_transient_gorgias_error(_http_error(404)) is False
        assert _is_transient_gorgias_error(_http_error(422)) is False

    def test_value_error_is_not_retryable(self):
        assert _is_transient_gorgias_error(ValueError("bad")) is False

    def test_generic_exception_is_not_retryable(self):
        assert _is_transient_gorgias_error(RuntimeError("something")) is False


# ── GorgiasNotConfigured guard ──────────────────────────────


class TestGorgiasNotConfigured:
    @patch("integrations.gorgias.settings")
    @pytest.mark.asyncio
    async def test_list_open_tickets_raises_when_disabled(self, mock_settings):
        mock_settings.GORGIAS_DOMAIN = ""
        mock_settings.GORGIAS_EMAIL = ""
        mock_settings.GORGIAS_API_KEY = None
        client = GorgiasClient()
        assert client.enabled is False
        with pytest.raises(GorgiasNotConfigured):
            await client.list_open_tickets()

    @patch("integrations.gorgias.settings")
    @pytest.mark.asyncio
    async def test_post_reply_raises_when_disabled(self, mock_settings):
        mock_settings.GORGIAS_DOMAIN = ""
        mock_settings.GORGIAS_EMAIL = ""
        mock_settings.GORGIAS_API_KEY = None
        client = GorgiasClient()
        with pytest.raises(GorgiasNotConfigured):
            await client.post_reply("123", "<p>Hello</p>")

    @patch("integrations.gorgias.settings")
    @pytest.mark.asyncio
    async def test_add_internal_note_raises_when_disabled(self, mock_settings):
        mock_settings.GORGIAS_DOMAIN = ""
        mock_settings.GORGIAS_EMAIL = ""
        mock_settings.GORGIAS_API_KEY = None
        client = GorgiasClient()
        with pytest.raises(GorgiasNotConfigured):
            await client.add_internal_note("123", "Note")

    @patch("integrations.gorgias.settings")
    @pytest.mark.asyncio
    async def test_get_ticket_messages_raises_when_disabled(self, mock_settings):
        mock_settings.GORGIAS_DOMAIN = ""
        mock_settings.GORGIAS_EMAIL = ""
        mock_settings.GORGIAS_API_KEY = None
        client = GorgiasClient()
        with pytest.raises(GorgiasNotConfigured):
            await client.get_ticket_messages("123")


# ── Webhook normalization ───────────────────────────────────


class TestNormalizeWebhookPayload:
    def test_normalizes_ticket_created_payload(self):
        payload = {
            "ticket": {
                "id": 999,
                "subject": "Where is my order?",
                "customer": {
                    "email": "alice@example.com",
                    "name": "Alice Smith",
                },
                "messages": [
                    {
                        "body_text": "I ordered 3 days ago and haven't received tracking.",
                        "stripped_text": "",
                    }
                ],
            }
        }
        ticket = GorgiasClient.normalize_webhook_payload(payload)
        assert ticket.id == "gorgias_999"
        assert ticket.gorgias_ticket_id == "999"
        assert ticket.customer_email == "alice@example.com"
        assert ticket.customer_name == "Alice Smith"
        assert ticket.subject == "Where is my order?"
        assert "3 days ago" in ticket.body

    def test_handles_missing_customer(self):
        payload = {
            "ticket": {
                "id": 1,
                "subject": "Test",
                "customer": None,
                "messages": [],
            }
        }
        ticket = GorgiasClient.normalize_webhook_payload(payload)
        assert ticket.customer_email == "unknown@example.com"
        assert ticket.customer_name is None

    def test_handles_missing_messages(self):
        payload = {
            "ticket": {
                "id": 2,
                "subject": "No messages",
                "customer": {"email": "bob@test.com"},
                "messages": [],
            }
        }
        ticket = GorgiasClient.normalize_webhook_payload(payload)
        assert ticket.body == ""

    def test_handles_missing_subject(self):
        payload = {
            "ticket": {
                "id": 3,
                "subject": None,
                "customer": {"email": "x@test.com"},
                "messages": [{"body_text": "Hello"}],
            }
        }
        ticket = GorgiasClient.normalize_webhook_payload(payload)
        assert ticket.subject == "(no subject)"

    def test_uses_stripped_text_fallback(self):
        payload = {
            "ticket": {
                "id": 4,
                "subject": "Test",
                "customer": {"email": "x@test.com"},
                "messages": [{"body_text": "", "stripped_text": "Stripped content"}],
            }
        }
        ticket = GorgiasClient.normalize_webhook_payload(payload)
        assert ticket.body == "Stripped content"

    def test_flat_payload_without_ticket_wrapper(self):
        payload = {
            "id": 5,
            "subject": "Flat",
            "customer": {"email": "flat@test.com"},
            "messages": [{"body_text": "Flat body"}],
        }
        ticket = GorgiasClient.normalize_webhook_payload(payload)
        assert ticket.id == "gorgias_5"
        assert ticket.subject == "Flat"


# ── Retry behaviour on transient errors ─────────────────────


class TestGorgiasRetryBehaviour:
    @patch("integrations.gorgias.settings")
    @pytest.mark.asyncio
    async def test_retry_on_5xx_eventually_succeeds(self, mock_settings):
        mock_settings.GORGIAS_DOMAIN = "test-store"
        mock_settings.GORGIAS_EMAIL = "test@gorgias.com"
        mock_settings.GORGIAS_API_KEY = MagicMock(get_secret_value=lambda: "fake-key")

        client = GorgiasClient()

        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise _http_error(503)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"data": [{"id": 1, "status": "open"}]}
            resp.raise_for_status = MagicMock()
            return resp

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(side_effect=mock_get)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_http

            result = await client.list_open_tickets()
            assert result == [{"id": 1, "status": "open"}]
            assert call_count == 3

    @patch("integrations.gorgias.settings")
    @pytest.mark.asyncio
    async def test_list_open_tickets_filters_client_side(self, mock_settings):
        """The API can't filter by status (400 'Unknown field') — the client must
        fetch without a status param and keep only open tickets locally."""
        mock_settings.GORGIAS_DOMAIN = "test-store"
        mock_settings.GORGIAS_EMAIL = "test@gorgias.com"
        mock_settings.GORGIAS_API_KEY = MagicMock(get_secret_value=lambda: "fake-key")

        client = GorgiasClient()
        captured_params = {}

        async def mock_get(url, **kwargs):
            captured_params.update(kwargs.get("params") or {})
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "data": [
                    {"id": 1, "status": "open"},
                    {"id": 2, "status": "pending"},
                    {"id": 3, "status": "solved"},
                    {"id": 4, "status": "open"},
                ]
            }
            resp.raise_for_status = MagicMock()
            return resp

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(side_effect=mock_get)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_http

            result = await client.list_open_tickets(limit=2)

        assert "status" not in captured_params
        assert captured_params.get("limit", 0) >= 60
        assert [t["id"] for t in result] == [1, 4]

    @patch("integrations.gorgias.settings")
    @pytest.mark.asyncio
    async def test_no_retry_on_4xx(self, mock_settings):
        mock_settings.GORGIAS_DOMAIN = "test-store"
        mock_settings.GORGIAS_EMAIL = "test@gorgias.com"
        mock_settings.GORGIAS_API_KEY = MagicMock(get_secret_value=lambda: "fake-key")

        client = GorgiasClient()

        async def mock_get(url, **kwargs):
            raise _http_error(401)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(side_effect=mock_get)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_http

            with pytest.raises(httpx.HTTPStatusError):
                await client.list_open_tickets()
            assert mock_http.get.await_count == 1
