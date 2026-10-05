"""Recharge admin REST API client (https://developer.rechargepayments.com).

Auth: X-Recharge-Access-Token header, one token per store.

API limitations this client works around (documented on purpose):
- **Pause**: Recharge exposes no pause endpoint. The merchant portal "pauses" by
  rescheduling the next charge, so we do the same via
  POST /subscriptions/{id}/change_next_charge_date (SUBSCRIPTION_PAUSE_DAYS ahead).
- **Skip**: skips operate on the *charge* (POST /addresses/{address_id}/charges/skip),
  which covers every subscription billed at that address/date — same semantics as
  Recharge's own portal. Requires an upcoming next_charge_scheduled_at.
- **Address**: subscriptions hang off an address, so an address edit is
  PUT /addresses/{address_id} and affects all subscriptions at that address.
- **Frequency**: PUT /subscriptions/{id} requires order_interval_unit,
  order_interval_frequency and charge_interval_frequency to be sent together.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from agent.config import settings
from agent.resilience import CircuitBreaker, guarded, register_breaker
from integrations.subscriptions import (
    NormalizedSubscription,
    SubscriptionAPIError,
    SubscriptionNotFound,
    _norm_frequency,
)

logger = structlog.get_logger(__name__)

RECHARGE_BREAKER = register_breaker(CircuitBreaker("recharge"))

_EXP_BACKOFF = {
    "stop": stop_after_attempt(3),
    "wait": wait_exponential(multiplier=1, min=1, max=8),
    "reraise": True,
}


def _is_transient(exc: BaseException) -> bool:
    """Retry 5xx/429/timeouts/connection errors only — 4xx means the request itself is wrong."""
    return isinstance(exc, httpx.TimeoutException | httpx.TransportError) or (
        isinstance(exc, httpx.HTTPStatusError)
        and (exc.response.status_code == 429 or 500 <= exc.response.status_code < 600)
    )


def _log_retry(retry_state) -> None:
    logger.warning(
        "recharge_retry",
        function=getattr(retry_state.fn, "__name__", str(retry_state.fn)),
        attempt=retry_state.attempt_number,
        error=str(retry_state.outcome.exception()),
    )


class RechargeClient:
    provider_name = "recharge"
    BASE_URL = "https://api.rechargeapps.com"

    def __init__(self, api_token: str | None = None):
        # None = process env (single-store / outside a request); a string =
        # store-scoped token from the registry ("" disables the client — agency
        # mode never falls back to another tenant's env token).
        if api_token is None:
            self.enabled = bool(settings.RECHARGE_API_TOKEN)
            token = (
                settings.RECHARGE_API_TOKEN.get_secret_value()
                if settings.RECHARGE_API_TOKEN
                else ""
            )
        else:
            token = str(api_token).strip()
            self.enabled = bool(token)
        if self.enabled:
            self.headers = {
                "X-Recharge-Access-Token": token,
                "Content-Type": "application/json",
            }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Public entry: callers see domain errors (SubscriptionAPIError) as
        always; retries + breaker happen on the raw HTTP worker below, which
        raises httpx.HTTPStatusError so the transient predicate can see status."""
        try:
            return await self._request_http(method, path, json_body=json_body, params=params)
        except httpx.HTTPStatusError as exc:
            raise SubscriptionAPIError(
                f"Recharge {method} {path} failed ({exc.response.status_code}): "
                f"{exc.response.text[:300]}"
            ) from exc

    @guarded(RECHARGE_BREAKER, _is_transient)
    @retry(
        retry=retry_if_exception(_is_transient),
        before_sleep=_log_retry,
        **_EXP_BACKOFF,
    )
    async def _request_http(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.request(
                method,
                f"{self.BASE_URL}{path}",
                headers=self.headers,
                json=json_body,
                params=params,
            )
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            if resp.status_code == 429 or resp.status_code >= 500:
                resp.raise_for_status()  # transient: tenacity retries, then re-raises
            raise SubscriptionAPIError(
                f"Recharge {method} {path} failed ({resp.status_code}): {resp.text[:300]}"
            )
        if not resp.content:
            return {}
        return resp.json()

    # ── Reads ──────────────────────────────────────────────

    async def _customer_id_for_email(self, email: str) -> int | None:
        data = await self._request("GET", "/customers", params={"email": email, "limit": 10})
        if not data:
            return None
        for cust in data.get("customers", []):
            if (cust.get("email") or "").lower() == email.lower():
                return int(cust["id"])
        return None

    async def _get_address(self, address_id: int) -> dict[str, Any] | None:
        return await self._request("GET", f"/addresses/{address_id}")

    def _normalize(
        self, sub: dict[str, Any], email: str | None, address: dict[str, Any] | None
    ) -> NormalizedSubscription:
        unit, count = _norm_frequency(
            sub.get("order_interval_unit"), sub.get("order_interval_frequency")
        )
        addr: dict[str, str] | None = None
        if address:
            addr = {
                "address1": str(address.get("address1") or ""),
                "address2": str(address.get("address2") or ""),
                "city": str(address.get("city") or ""),
                "state": str(address.get("province") or address.get("state") or ""),
                "zip": str(address.get("zip") or ""),
                "country": str(address.get("country") or ""),
                "name": f"{address.get('first_name') or ''} {address.get('last_name') or ''}".strip(),
                "phone": str(address.get("phone") or ""),
            }
        return NormalizedSubscription(
            id=str(sub.get("id")),
            provider="recharge",
            status=str(sub.get("status") or ""),
            title=str(sub.get("product_title") or "Subscription"),
            quantity=sub.get("quantity"),
            price=str(sub.get("price")) if sub.get("price") is not None else None,
            next_charge_date=sub.get("next_charge_scheduled_at"),
            frequency_unit=unit,
            frequency_count=count,
            address=addr,
            email=email,
            raw=sub,
        )

    async def list_subscriptions(self, email: str) -> list[NormalizedSubscription]:
        customer_id = await self._customer_id_for_email(email)
        if customer_id is None:
            return []
        data = await self._request(
            "GET", "/subscriptions", params={"customer_id": customer_id, "limit": 50}
        )
        subs = (data or {}).get("subscriptions", [])
        out = []
        for sub in subs:
            address = await self._get_address(sub["address_id"]) if sub.get("address_id") else None
            out.append(self._normalize(sub, email, address))
        return out

    async def get_subscription(self, subscription_id: str) -> NormalizedSubscription | None:
        data = await self._request("GET", f"/subscriptions/{subscription_id}")
        if not data:
            return None
        sub = data.get("subscription", data)
        address = await self._get_address(sub["address_id"]) if sub.get("address_id") else None
        # Recharge addresses don't carry the customer email — callers (ticket routes)
        # already know it, so leave it unset here.
        return self._normalize(sub, None, address)

    # ── Writes ─────────────────────────────────────────────

    async def pause(self, sub: NormalizedSubscription) -> None:
        # No pause endpoint exists — push the next charge out (portal behavior).
        days = max(1, int(settings.SUBSCRIPTION_PAUSE_DAYS))
        new_date = (datetime.now(UTC) + timedelta(days=days)).date().isoformat()
        result = await self._request(
            "POST",
            f"/subscriptions/{sub.id}/change_next_charge_date",
            json_body={"date": new_date},
        )
        if result is None:
            raise SubscriptionNotFound(f"Recharge subscription {sub.id} not found")
        logger.info("recharge_subscription_paused", subscription_id=sub.id, resume_by=new_date)

    async def skip(self, sub: NormalizedSubscription) -> None:
        address_id = (sub.raw or {}).get("address_id")
        charge_date = sub.next_charge_date
        if not address_id or not charge_date:
            raise SubscriptionAPIError(
                f"Recharge subscription {sub.id} has no address/next-charge date to skip"
            )
        date_only = str(charge_date)[:10]
        result = await self._request(
            "POST",
            f"/addresses/{address_id}/charges/skip",
            json_body={"date": date_only},
        )
        if result is None:
            raise SubscriptionAPIError(
                f"Recharge skip failed: no charge found on {date_only} for address {address_id}"
            )
        logger.info("recharge_subscription_skipped", subscription_id=sub.id, date=date_only)

    async def cancel(self, sub: NormalizedSubscription, reason: str) -> None:
        result = await self._request(
            "POST",
            f"/subscriptions/{sub.id}/cancel",
            json_body={"cancellation_reason": reason[:500]},
        )
        if result is None:
            raise SubscriptionNotFound(f"Recharge subscription {sub.id} not found")
        logger.info("recharge_subscription_cancelled", subscription_id=sub.id, reason=reason)

    async def update_address(self, sub: NormalizedSubscription, address: dict[str, str]) -> None:
        address_id = (sub.raw or {}).get("address_id")
        if not address_id:
            raise SubscriptionAPIError(f"Recharge subscription {sub.id} has no address to update")
        body: dict[str, Any] = {}
        mapping = {
            "address1": "address1",
            "address2": "address2",
            "city": "city",
            "zip": "zip",
            "country": "country",
            "phone": "phone",
            "company": "company",
        }
        for src, dst in mapping.items():
            if address.get(src) is not None:
                body[dst] = address[src]
        # Recharge stores the region as `province`; accept `state` from the UI too.
        region = address.get("state") or address.get("province")
        if region:
            body["province"] = region
        if address.get("name"):
            parts = str(address["name"]).split(None, 1)
            body["first_name"] = parts[0]
            if len(parts) > 1:
                body["last_name"] = parts[1]
        elif address.get("first_name"):
            body["first_name"] = address["first_name"]
            body["last_name"] = address.get("last_name", "")
        result = await self._request("PUT", f"/addresses/{address_id}", json_body=body)
        if result is None:
            raise SubscriptionNotFound(f"Recharge address {address_id} not found")
        logger.info("recharge_subscription_address_updated", subscription_id=sub.id)

    async def change_frequency(self, sub: NormalizedSubscription, unit: str, count: int) -> None:
        # Recharge requires all three interval fields together.
        body = {
            "order_interval_unit": unit,
            "order_interval_frequency": str(count),
            "charge_interval_frequency": str(count),
        }
        result = await self._request("PUT", f"/subscriptions/{sub.id}", json_body=body)
        if result is None:
            raise SubscriptionNotFound(f"Recharge subscription {sub.id} not found")
        logger.info(
            "recharge_subscription_frequency_changed",
            subscription_id=sub.id,
            unit=unit,
            count=count,
        )
