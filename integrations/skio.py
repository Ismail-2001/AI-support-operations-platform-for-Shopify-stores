"""Skio GraphQL API client (https://code.skio.com).

Endpoint: POST https://graphql.skio.com/v1/graphql
Auth:     `authorization: API <token>` header (case-sensitive per Skio docs),
          Content-Type: application/graphql (raw GraphQL document as the body).

Skio documents these limits and we respect them: query depth <= 4, max 100 nodes
per request, 2000 requests/minute/token. All lookup queries here fetch <= 5
subscriptions and <= 5 line items each — far under the node cap.

Variable values are embedded as JSON-escaped literals (json.dumps) so customer
input can never break out of the document structure.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from agent.config import settings
from integrations.subscriptions import (
    NormalizedSubscription,
    SubscriptionAPIError,
    _norm_frequency,
)

logger = structlog.get_logger(__name__)

_EXP_BACKOFF = {
    "stop": stop_after_attempt(3),
    "wait": wait_exponential(multiplier=1, min=1, max=8),
}

_SUB_FIELDS = """
    id
    platformId
    status
    nextBillingDate
    cancelledAt
    createdAt
    StorefrontUser { email }
    ShippingAddress { address1 address2 city province zip country firstName lastName phoneNumber }
    BillingPolicy { interval intervalCount }
    DeliveryPolicy { interval intervalCount }
    SubscriptionLines(where: {removedAt: {_is_null: true}}, limit: 5) {
        quantity
        priceWithoutDiscount
        ProductVariant { title Product { title } }
    }
"""


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, httpx.TimeoutException | httpx.TransportError) or (
        isinstance(exc, httpx.HTTPStatusError) and 500 <= exc.response.status_code < 600
    )


def _log_retry(retry_state) -> None:
    logger.warning(
        "skio_retry",
        function=getattr(retry_state.fn, "__name__", str(retry_state.fn)),
        attempt=retry_state.attempt_number,
        error=str(retry_state.outcome.exception()),
    )


class SkioClient:
    provider_name = "skio"
    BASE_URL = "https://graphql.skio.com/v1/graphql"

    def __init__(self, api_token: str | None = None):
        # None = process env; a string = store-scoped token from the registry
        # ("" disables the client — no cross-tenant env fallback).
        if api_token is None:
            self.enabled = bool(settings.SKIO_API_TOKEN)
            token = (
                settings.SKIO_API_TOKEN.get_secret_value() if settings.SKIO_API_TOKEN else ""
            )
        else:
            token = str(api_token).strip()
            self.enabled = bool(token)
        if self.enabled:
            self.headers = {
                "authorization": f"API {token}",
                "Content-Type": "application/graphql",
            }

    @retry(
        retry=retry_if_exception(_is_transient),
        before_sleep=_log_retry,
        **_EXP_BACKOFF,
    )
    async def _gql(self, document: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                self.BASE_URL, headers=self.headers, content=document.encode("utf-8")
            )
        if resp.status_code >= 400:
            raise SubscriptionAPIError(
                f"Skio request failed ({resp.status_code}): {resp.text[:300]}"
            )
        payload = resp.json()
        errors = payload.get("errors")
        if errors:
            messages = "; ".join(str(e.get("message", e)) for e in errors)
            raise SubscriptionAPIError(f"Skio GraphQL error: {messages[:400]}")
        return payload.get("data") or {}

    # ── Reads ──────────────────────────────────────────────

    def _normalize(self, sub: dict[str, Any]) -> NormalizedSubscription:
        lines = sub.get("SubscriptionLines") or []
        title = "Subscription"
        price = None
        quantity = None
        if lines:
            first = lines[0]
            product = (first.get("ProductVariant") or {}).get("Product") or {}
            variant = (first.get("ProductVariant") or {}).get("title")
            pieces = [p for p in (product.get("title"), variant) if p]
            title = " — ".join(dict.fromkeys(pieces)) if pieces else "Subscription"
            price = (
                str(first["priceWithoutDiscount"])
                if first.get("priceWithoutDiscount") is not None
                else None
            )
            quantity = first.get("quantity")
        policy = sub.get("BillingPolicy") or sub.get("DeliveryPolicy") or {}
        unit, count = _norm_frequency(policy.get("interval"), policy.get("intervalCount"))
        addr_raw = sub.get("ShippingAddress")
        addr: dict[str, str] | None = None
        if addr_raw:
            addr = {
                "address1": str(addr_raw.get("address1") or ""),
                "address2": str(addr_raw.get("address2") or ""),
                "city": str(addr_raw.get("city") or ""),
                "state": str(addr_raw.get("province") or ""),
                "zip": str(addr_raw.get("zip") or ""),
                "country": str(addr_raw.get("country") or ""),
                "name": f"{addr_raw.get('firstName') or ''} {addr_raw.get('lastName') or ''}".strip(),
                "phone": str(addr_raw.get("phoneNumber") or ""),
            }
        next_date = sub.get("nextBillingDate")
        return NormalizedSubscription(
            id=str(sub.get("id")),
            provider="skio",
            status=str(sub.get("status") or ""),
            title=title,
            quantity=quantity,
            price=price,
            next_charge_date=str(next_date)[:10] if next_date else None,
            frequency_unit=unit,
            frequency_count=count,
            address=addr,
            email=(sub.get("StorefrontUser") or {}).get("email"),
            raw=sub,
        )

    async def list_subscriptions(self, email: str) -> list[NormalizedSubscription]:
        email_lit = json.dumps(email)
        document = (
            "query { Subscriptions("
            f"where: {{StorefrontUser: {{email: {{_eq: {email_lit}}}}}}}, "
            "limit: 5, order_by: {createdAt: desc}) "
            f"{_SUB_FIELDS} }}"
        )
        data = await self._gql(document)
        return [self._normalize(s) for s in data.get("Subscriptions", [])]

    async def get_subscription(self, subscription_id: str) -> NormalizedSubscription | None:
        id_lit = json.dumps(subscription_id)
        document = f"query {{ SubscriptionByPk(id: {id_lit}) {_SUB_FIELDS} }}"
        data = await self._gql(document)
        sub = data.get("SubscriptionByPk")
        return self._normalize(sub) if sub else None

    # ── Mutations ──────────────────────────────────────────

    async def _mutation(self, name: str, input_obj: str, selection: str) -> dict[str, Any]:
        document = f"mutation {{ {name}(input: {input_obj}) {{ {selection} }} }}"
        data = await self._gql(document)
        return data.get(name) or {}

    def _require_ok(self, name: str, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            message = result.get("message") or "unknown error"
            raise SubscriptionAPIError(f"Skio {name} failed: {message}")

    async def pause(self, sub: NormalizedSubscription) -> None:
        result = await self._mutation(
            "pauseSubscription", f'{{subscriptionId: "{sub.id}"}}', "ok message"
        )
        self._require_ok("pauseSubscription", result)
        logger.info("skio_subscription_paused", subscription_id=sub.id)

    async def skip(self, sub: NormalizedSubscription) -> None:
        result = await self._mutation(
            "skipSubscription", f'{{subscriptionId: "{sub.id}"}}', "ok message nextBillingDate"
        )
        self._require_ok("skipSubscription", result)
        logger.info("skio_subscription_skipped", subscription_id=sub.id)

    async def cancel(self, sub: NormalizedSubscription, reason: str) -> None:
        # Skio's cancel input has no reason field — the cancel reason lives in the
        # store's cancel flow; shouldSendNotif lets Skio email the customer per
        # the store's notification settings. The reason is recorded in our audit.
        result = await self._mutation(
            "cancelSubscription",
            f'{{subscriptionId: "{sub.id}", shouldSendNotif: true}}',
            "ok",
        )
        if not result.get("ok"):
            raise SubscriptionAPIError("Skio cancelSubscription failed")
        logger.info("skio_subscription_cancelled", subscription_id=sub.id, reason=reason)

    async def update_address(self, sub: NormalizedSubscription, address: dict[str, str]) -> None:
        fields = []
        mapping = {
            "address1": "address1",
            "address2": "address2",
            "city": "city",
            "zip": "zip",
            "country": "country",
            "phone": "phone",
        }
        for src, dst in mapping.items():
            if address.get(src) is not None:
                fields.append(f"{dst}: {json.dumps(address[src])}")
        region = address.get("state") or address.get("province")
        if region:
            fields.append(f"province: {json.dumps(region)}")
        if address.get("name"):
            parts = str(address["name"]).split(None, 1)
            fields.append(f"firstName: {json.dumps(parts[0])}")
            if len(parts) > 1:
                fields.append(f"lastName: {json.dumps(parts[1])}")
        fields.insert(0, f'subscriptionId: "{sub.id}"')
        result = await self._mutation(
            "updateSubscriptionShippingAddress", "{" + ", ".join(fields) + "}", "ok message"
        )
        self._require_ok("updateSubscriptionShippingAddress", result)
        logger.info("skio_subscription_address_updated", subscription_id=sub.id)

    async def change_frequency(self, sub: NormalizedSubscription, unit: str, count: int) -> None:
        # Skio's interval vocabulary is upper-case (DAY/WEEK/MONTH/YEAR).
        interval = unit.upper()
        result = await self._mutation(
            "subscriptionEditInterval",
            f'{{subscriptionId: "{sub.id}", billingInterval: "{interval}", '
            f"billingIntervalCount: {int(count)}}}",
            "subscriptionId",
        )
        if not result.get("subscriptionId"):
            raise SubscriptionAPIError("Skio subscriptionEditInterval failed")
        logger.info(
            "skio_subscription_frequency_changed",
            subscription_id=sub.id,
            unit=interval,
            count=count,
        )

    async def generate_magic_link(self, email: str, return_to_path: str = "/subscriptions") -> str:
        """Passwordless portal link — used for 'update my payment method' requests,
        which are always customer-self-service (we never touch card data)."""
        document = (
            "mutation { generateMagicLink(input: "
            f"{{email: {json.dumps(email)}, returnToPath: {json.dumps(return_to_path)}}}"
            ") { ok magicLinkUrl error } }"
        )
        data = await self._gql(document)
        result = data.get("generateMagicLink") or {}
        if not result.get("ok") or not result.get("magicLinkUrl"):
            raise SubscriptionAPIError(
                f"Skio magic link failed: {result.get('error') or 'unknown error'}"
            )
        return str(result["magicLinkUrl"])
