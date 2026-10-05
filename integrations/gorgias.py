"""Minimal Gorgias REST API client — fetch tickets, post replies, normalize into our SupportTicket model."""

from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from agent.config import settings
from agent.models import SupportTicket, TicketChannel
from agent.resilience import CircuitBreaker, guarded, register_breaker

logger = structlog.get_logger(__name__)

GORGIAS_BREAKER = register_breaker(CircuitBreaker("gorgias"))


class GorgiasNotConfigured(Exception):
    pass


_EXP_BACKOFF = {
    "stop": stop_after_attempt(3),
    "wait": wait_exponential(multiplier=1, min=1, max=8),
    "reraise": True,
}


def _is_transient_gorgias_error(exc: BaseException) -> bool:
    """Transient = 5xx server error, timeout, or connection error.
    4xx client errors are never retried — they mean the request itself is wrong."""
    return isinstance(exc, httpx.TimeoutException | httpx.TransportError) or (
        isinstance(exc, httpx.HTTPStatusError)
        and (exc.response.status_code == 429 or 500 <= exc.response.status_code < 600)
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
    """Scope-aware Gorgias client.

    Outside a store scope, credentials come from .env (read live, so settings
    applied after construction - tests - are honored). Inside one, credentials
    come ONLY from that store's registry record - a registered tenant's
    Gorgias account never falls back to the deployment env, and each call
    re-reads the active scope (the same client instance serves every store;
    ContextVars make it safe)."""

    @staticmethod
    def _from_env() -> tuple[bool, str, tuple[str, str]]:
        enabled = bool(
            settings.GORGIAS_DOMAIN and settings.GORGIAS_EMAIL and settings.GORGIAS_API_KEY
        )
        if not enabled:
            return (False, "", ("", ""))
        return (
            True,
            f"https://{settings.GORGIAS_DOMAIN}.gorgias.com/api",
            (settings.GORGIAS_EMAIL, settings.GORGIAS_API_KEY.get_secret_value()),
        )

    @staticmethod
    def _from_scope() -> tuple[bool, str, tuple[str, str]] | None:
        from agent.multistore import integration_overrides

        overrides = integration_overrides()
        if overrides is None:
            return None  # no store context - use env
        domain = overrides.get("gorgias_domain") or ""
        email = overrides.get("gorgias_email") or ""
        key = overrides.get("gorgias_api_key") or ""
        # Store scope NEVER merges with env: partial config = disabled here.
        return (
            bool(domain and email and key),
            f"https://{domain}.gorgias.com/api" if domain else "",
            (email, key),
        )

    @property
    def _creds(self) -> tuple[bool, str, tuple[str, str]]:
        # NOTE: an in-scope tuple is always truthy (length 3) even when
        # enabled=False - that's what keeps it from falling through to env.
        return self._from_scope() or self._from_env()

    @property
    def enabled(self) -> bool:
        return self._creds[0]

    @property
    def base_url(self) -> str:
        return self._creds[1]

    @property
    def auth(self) -> tuple[str, str]:
        return self._creds[2]

    @guarded(GORGIAS_BREAKER, _is_transient_gorgias_error)
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

    @guarded(GORGIAS_BREAKER, _is_transient_gorgias_error)
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

    @guarded(GORGIAS_BREAKER, _is_transient_gorgias_error)
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

    @guarded(GORGIAS_BREAKER, _is_transient_gorgias_error)
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
