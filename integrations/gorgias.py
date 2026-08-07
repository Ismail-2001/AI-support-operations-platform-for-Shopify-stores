"""Minimal Gorgias REST API client — fetch tickets, post replies, normalize into our SupportTicket model."""

from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from agent.config import settings
from agent.models import SupportTicket, TicketChannel

logger = structlog.get_logger(__name__)


class GorgiasNotConfigured(Exception):
    pass


_EXP_BACKOFF = {
    "stop": stop_after_attempt(3),
    "wait": wait_exponential(multiplier=1, min=1, max=8),
}


def _is_transient_gorgias_error(exc: BaseException) -> bool:
    """Transient = 5xx server error, timeout, or connection error.
    4xx client errors are never retried — they mean the request itself is wrong."""
    return (
        isinstance(exc, httpx.TimeoutException | httpx.TransportError)
        or (isinstance(exc, httpx.HTTPStatusError) and 500 <= exc.response.status_code < 600)
    )


def _log_retry_attempt(retry_state) -> None:
    fn_name = getattr(retry_state.fn, "__name__", str(retry_state.fn))
    logger.warning(
        "gorgias_retry",
        function=fn_name,
        attempt=retry_state.attempt_number,
        error=str(retry_state.outcome.exception()),
        error_type=type(retry_state.outcome.exception()).__name__,
    )


class GorgiasClient:
    def __init__(self):
        self.enabled = bool(
            settings.GORGIAS_DOMAIN and settings.GORGIAS_EMAIL and settings.GORGIAS_API_KEY
        )
        if self.enabled:
            self.base_url = f"https://{settings.GORGIAS_DOMAIN}.gorgias.com/api"
            self.auth = (settings.GORGIAS_EMAIL, settings.GORGIAS_API_KEY.get_secret_value())

    @retry(
        retry=retry_if_exception(_is_transient_gorgias_error),
        before_sleep=_log_retry_attempt,
        **_EXP_BACKOFF,
    )
    async def list_open_tickets(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.enabled:
            raise GorgiasNotConfigured("Gorgias credentials not set in .env")
        async with httpx.AsyncClient(timeout=15, auth=self.auth) as client:
            resp = await client.get(
                f"{self.base_url}/tickets",
                params={"status": "open", "limit": limit},
            )
            resp.raise_for_status()
            return resp.json().get("data", [])

    @retry(
        retry=retry_if_exception(_is_transient_gorgias_error),
        before_sleep=_log_retry_attempt,
        **_EXP_BACKOFF,
    )
    async def get_ticket_messages(self, ticket_id: str) -> list[dict[str, Any]]:
        if not self.enabled:
            raise GorgiasNotConfigured("Gorgias credentials not set in .env")
        async with httpx.AsyncClient(timeout=15, auth=self.auth) as client:
            resp = await client.get(f"{self.base_url}/tickets/{ticket_id}/messages")
            resp.raise_for_status()
            return resp.json().get("data", [])

    @retry(
        retry=retry_if_exception(_is_transient_gorgias_error),
        before_sleep=_log_retry_attempt,
        **_EXP_BACKOFF,
    )
    async def post_reply(
        self, ticket_id: str, body_html: str, channel: str = "email"
    ) -> dict[str, Any]:
        """Posts a reply that actually sends to the customer. Only call this for auto-send-eligible tickets."""
        if not self.enabled:
            raise GorgiasNotConfigured("Gorgias credentials not set in .env")
        async with httpx.AsyncClient(timeout=15, auth=self.auth) as client:
            resp = await client.post(
                f"{self.base_url}/tickets/{ticket_id}/messages",
                json={
                    "channel": channel,
                    "via": channel,
                    "from_agent": True,
                    "body_html": body_html,
                    "source": {"type": channel},
                },
            )
            resp.raise_for_status()
            return resp.json()

    @retry(
        retry=retry_if_exception(_is_transient_gorgias_error),
        before_sleep=_log_retry_attempt,
        **_EXP_BACKOFF,
    )
    async def add_internal_note(self, ticket_id: str, note: str) -> dict[str, Any]:
        """For low-confidence tickets: attach the AI draft as an internal note instead of sending it."""
        if not self.enabled:
            raise GorgiasNotConfigured("Gorgias credentials not set in .env")
        async with httpx.AsyncClient(timeout=15, auth=self.auth) as client:
            resp = await client.post(
                f"{self.base_url}/tickets/{ticket_id}/messages",
                json={
                    "channel": "internal-note",
                    "via": "internal-note",
                    "from_agent": True,
                    "body_html": note,
                    "source": {"type": "internal-note"},
                },
            )
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def normalize_webhook_payload(payload: dict[str, Any]) -> SupportTicket:
        """Gorgias 'ticket-created' webhook payload -> our SupportTicket model."""
        ticket = payload.get("ticket", payload)
        customer = ticket.get("customer", {}) or {}
        messages = ticket.get("messages", []) or []
        first_message = messages[0] if messages else {}

        return SupportTicket(
            id=f"gorgias_{ticket.get('id')}",
            gorgias_ticket_id=str(ticket.get("id")),
            customer_email=customer.get("email", "unknown@example.com"),
            customer_name=customer.get("name"),
            subject=ticket.get("subject") or "(no subject)",
            body=first_message.get("body_text") or first_message.get("stripped_text") or "",
            channel=TicketChannel.GORGIAS,
        )
