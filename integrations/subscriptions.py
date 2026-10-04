"""Subscription management providers (Recharge + Skio) behind one facade.

The agent and API never talk to a provider directly — they go through
SubscriptionService, which normalizes both providers into NormalizedSubscription
and enforces state guards (no cancelling a cancelled subscription, no skipping
when there's no upcoming charge) before anything is executed.

Provider selection: settings.SUBSCRIPTION_PROVIDER — "auto" (default) picks
Recharge when its token is set, else Skio; "recharge"/"skio" force a choice.
When nothing is configured every call raises SubscriptionNotConfigured, which
the API maps to 409 SUBSCRIPTION_NOT_CONNECTED and the response prompt tells
the agent to say so plainly and escalate.
"""

from __future__ import annotations

from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field

from agent.config import settings

logger = structlog.get_logger(__name__)

ProviderName = Literal["recharge", "skio"]


class SubscriptionError(Exception):
    """Base class — carries the API error code and HTTP status the route should use."""

    code = "SUBSCRIPTION_ERROR"
    status = 502

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


class SubscriptionNotConfigured(SubscriptionError):
    code = "SUBSCRIPTION_NOT_CONNECTED"
    status = 409


class SubscriptionNotFound(SubscriptionError):
    code = "SUBSCRIPTION_NOT_FOUND"
    status = 404


class SubscriptionInvalidState(SubscriptionError):
    code = "SUBSCRIPTION_INVALID_STATE"
    status = 409


class SubscriptionAPIError(SubscriptionError):
    code = "SUBSCRIPTION_API_ERROR"
    status = 502


class NormalizedSubscription(BaseModel):
    """Provider-agnostic view of one subscription — what the dashboard renders."""

    id: str
    provider: ProviderName
    status: str
    title: str
    quantity: int | None = None
    price: str | None = None
    next_charge_date: str | None = None  # ISO date (YYYY-MM-DD) or full ISO timestamp
    frequency_unit: str | None = None  # "day" | "week" | "month" (lowercased)
    frequency_count: int | None = None
    address: dict[str, str] | None = None
    email: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_cancelled(self) -> bool:
        return self.status.upper().startswith("CANCEL")


def _norm_frequency(unit: Any, count: Any) -> tuple[str | None, int | None]:
    u = str(unit).strip().lower() if unit else None
    if u and u.endswith("s") and u not in ("days",):  # "weeks"/"months" -> singular
        u = u[:-1]
    try:
        c = int(count) if count is not None else None
    except (TypeError, ValueError):
        c = None
    return (u, c)


class SubscriptionService:
    """Facade: picks the configured provider and applies shared state guards."""

    def __init__(self, provider: str | None = None):
        self.requested = (provider or settings.SUBSCRIPTION_PROVIDER or "auto").lower()
        self._client: Any = None
        self.provider: ProviderName | None = None
        self._resolve()

    def _resolve(self) -> None:
        from integrations.recharge import RechargeClient
        from integrations.skio import SkioClient

        recharge = RechargeClient()
        skio = SkioClient()
        if self.requested == "recharge":
            candidates = [recharge]
        elif self.requested == "skio":
            candidates = [skio]
        else:  # auto — first connected wins
            candidates = [recharge, skio]
        for client in candidates:
            if client.enabled:
                self._client = client
                self.provider = client.provider_name
                return
        # Nothing connected (or the forced choice isn't configured).
        self._client = None
        self.provider = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @property
    def connected_providers(self) -> list[str]:
        from integrations.recharge import RechargeClient
        from integrations.skio import SkioClient

        out = []
        if RechargeClient().enabled:
            out.append("recharge")
        if SkioClient().enabled:
            out.append("skio")
        return out

    def _require(self) -> Any:
        if self._client is None:
            wanted = self.requested
            raise SubscriptionNotConfigured(
                f"No subscription app connected (SUBSCRIPTION_PROVIDER={wanted}) — "
                f"connect Recharge or Skio in settings before managing subscriptions"
            )
        return self._client

    # ── Reads ──────────────────────────────────────────────

    async def list_subscriptions(self, email: str) -> list[NormalizedSubscription]:
        return await self._require().list_subscriptions(email)

    async def get_subscription(self, subscription_id: str) -> NormalizedSubscription:
        sub = await self._require().get_subscription(subscription_id)
        if sub is None:
            raise SubscriptionNotFound(f"Subscription {subscription_id} not found")
        return sub

    # ── Writes (all state-guarded) ─────────────────────────

    def _guard_not_cancelled(self, sub: NormalizedSubscription) -> None:
        if sub.is_cancelled:
            raise SubscriptionInvalidState(
                f"Subscription {sub.id} is already cancelled — nothing to do",
                details={"status": sub.status},
            )

    async def pause(self, subscription_id: str) -> NormalizedSubscription:
        sub = await self.get_subscription(subscription_id)
        self._guard_not_cancelled(sub)
        await self._client.pause(sub)
        return await self.get_subscription(subscription_id)

    async def skip(self, subscription_id: str) -> NormalizedSubscription:
        sub = await self.get_subscription(subscription_id)
        self._guard_not_cancelled(sub)
        if not sub.next_charge_date:
            raise SubscriptionInvalidState(
                f"Subscription {sub.id} has no upcoming charge to skip",
                details={"status": sub.status},
            )
        await self._client.skip(sub)
        return await self.get_subscription(subscription_id)

    async def cancel(
        self, subscription_id: str, reason: str = "Requested by customer"
    ) -> NormalizedSubscription:
        sub = await self.get_subscription(subscription_id)
        self._guard_not_cancelled(sub)
        await self._client.cancel(sub, reason)
        return await self.get_subscription(subscription_id)

    async def update_address(
        self, subscription_id: str, address: dict[str, str]
    ) -> NormalizedSubscription:
        sub = await self.get_subscription(subscription_id)
        self._guard_not_cancelled(sub)
        await self._client.update_address(sub, address)
        return await self.get_subscription(subscription_id)

    async def change_frequency(
        self, subscription_id: str, unit: str, count: int
    ) -> NormalizedSubscription:
        sub = await self.get_subscription(subscription_id)
        self._guard_not_cancelled(sub)
        norm_unit, _ = _norm_frequency(unit, count)
        if norm_unit not in ("day", "week", "month"):
            raise SubscriptionInvalidState(
                f"Unsupported frequency unit '{unit}' — use day, week, or month",
                details={"unit": unit},
            )
        if count < 1 or count > 60:
            raise SubscriptionInvalidState(
                f"Frequency count {count} out of range (1-60)", details={"count": count}
            )
        await self._client.change_frequency(sub, norm_unit, int(count))
        return await self.get_subscription(subscription_id)
