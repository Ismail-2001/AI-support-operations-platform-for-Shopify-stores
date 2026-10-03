"""Minimal Shopify Admin API client — just what the support agent needs: order lookup."""

from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from agent.config import settings

logger = structlog.get_logger(__name__)


class ShopifyNotConfigured(Exception):
    pass


_EXP_BACKOFF = {
    "stop": stop_after_attempt(3),
    "wait": wait_exponential(multiplier=1, min=1, max=8),
}


def _is_transient_shopify_error(exc: BaseException) -> bool:
    """Transient = 5xx server error, timeout, or connection error.
    4xx client errors are never retried — they mean the request itself is wrong."""
    return isinstance(exc, httpx.TimeoutException | httpx.TransportError) or (
        isinstance(exc, httpx.HTTPStatusError) and 500 <= exc.response.status_code < 600
    )


def _is_timeout_or_connection_error(exc: BaseException) -> bool:
    """Strict predicate for create_refund — never retry a 5xx or 4xx, since the
    refund *may* have been accepted by Shopify even if the response was lost."""
    return isinstance(exc, httpx.TimeoutException | httpx.TransportError)


def _log_retry_attempt(retry_state) -> None:
    fn_name = getattr(retry_state.fn, "__name__", str(retry_state.fn))
    logger.warning(
        "shopify_retry",
        function=fn_name,
        attempt=retry_state.attempt_number,
        error=str(retry_state.outcome.exception()),
        error_type=type(retry_state.outcome.exception()).__name__,
    )


class ShopifyClient:
    def __init__(self):
        self.enabled = bool(settings.SHOPIFY_SHOP_DOMAIN and settings.SHOPIFY_ACCESS_TOKEN)
        if self.enabled:
            self.base_url = (
                f"https://{settings.SHOPIFY_SHOP_DOMAIN}/admin/api/{settings.SHOPIFY_API_VERSION}"
            )
            self.headers = {
                "X-Shopify-Access-Token": settings.SHOPIFY_ACCESS_TOKEN.get_secret_value(),
                "Content-Type": "application/json",
            }

    @retry(
        retry=retry_if_exception(_is_transient_shopify_error),
        before_sleep=_log_retry_attempt,
        **_EXP_BACKOFF,
    )
    async def get_order_by_number(self, order_number: str) -> dict[str, Any] | None:
        """order_number can be '1042', '#1042', or 'ORD-1042' — we normalize to Shopify's 'name' filter."""
        if not self.enabled:
            raise ShopifyNotConfigured("Shopify credentials not set in .env")

        name = order_number.strip().lstrip("#")
        if not name.startswith("#"):
            name = f"#{name}"

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/orders.json",
                headers=self.headers,
                params={"name": name, "status": "any"},
            )
            resp.raise_for_status()
            orders = resp.json().get("orders", [])
            return orders[0] if orders else None

    @retry(
        retry=retry_if_exception(_is_transient_shopify_error),
        before_sleep=_log_retry_attempt,
        **_EXP_BACKOFF,
    )
    async def get_recent_orders_by_email(self, email: str, limit: int = 3) -> list[dict[str, Any]]:
        if not self.enabled:
            raise ShopifyNotConfigured("Shopify credentials not set in .env")

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/orders.json",
                headers=self.headers,
                params={
                    "email": email,
                    "status": "any",
                    "limit": limit,
                    "order": "created_at desc",
                },
            )
            resp.raise_for_status()
            return resp.json().get("orders", [])

    @retry(
        retry=retry_if_exception(_is_transient_shopify_error),
        before_sleep=_log_retry_attempt,
        **_EXP_BACKOFF,
    )
    async def get_order_by_id(self, order_id: str) -> dict[str, Any] | None:
        if not self.enabled:
            raise ShopifyNotConfigured("Shopify credentials not set in .env")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{self.base_url}/orders/{order_id}.json", headers=self.headers)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json().get("order")

    @retry(
        retry=retry_if_exception(_is_transient_shopify_error),
        before_sleep=_log_retry_attempt,
        **_EXP_BACKOFF,
    )
    async def get_shop_policies(self) -> dict[str, str]:
        """Shopify's legacy policies.json endpoint — Settings > Policies content."""
        if not self.enabled:
            raise ShopifyNotConfigured("Shopify credentials not set in .env")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{self.base_url}/policies.json", headers=self.headers)
            resp.raise_for_status()
            policies = resp.json().get("policies", [])
            return {p["title"]: p.get("body", "") for p in policies if p.get("body")}

    @retry(
        retry=retry_if_exception(_is_transient_shopify_error),
        before_sleep=_log_retry_attempt,
        **_EXP_BACKOFF,
    )
    async def get_products(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.enabled:
            raise ShopifyNotConfigured("Shopify credentials not set in .env")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/products.json",
                headers=self.headers,
                params={"limit": limit, "status": "active"},
            )
            resp.raise_for_status()
            return resp.json().get("products", [])

    @retry(
        retry=retry_if_exception(_is_timeout_or_connection_error),
        before_sleep=_log_retry_attempt,
        **_EXP_BACKOFF,
    )
    async def create_refund(
        self, order_id: str, amount: float, reason: str = "", notify_customer: bool = True
    ) -> dict[str, Any]:
        """Creates a monetary refund on an order. ALWAYS call this only after explicit human
        approval — see api/customer_support.py POST /tickets/{id}/actions/refund. This method
        itself does not gate on anything; the safety gate lives at the API layer."""
        if not self.enabled:
            raise ShopifyNotConfigured("Shopify credentials not set in .env")

        async with httpx.AsyncClient(timeout=20) as client:
            # Shopify requires calculating refund via the /calculate endpoint first for
            # transaction-based refunds; for a straightforward monetary refund we can
            # submit a refund with an explicit transaction amount against the order's
            # original gateway transaction.
            order_resp = await client.get(
                f"{self.base_url}/orders/{order_id}/transactions.json", headers=self.headers
            )
            order_resp.raise_for_status()
            transactions = order_resp.json().get("transactions", [])
            parent_txn = next((t for t in transactions if t.get("kind") == "sale"), None)
            if not parent_txn:
                raise ValueError(f"No sale transaction found on order {order_id} to refund against")

            payload = {
                "refund": {
                    "notify": notify_customer,
                    "note": reason or "Refund issued via AI support agent (human-approved)",
                    "transactions": [
                        {
                            "parent_id": parent_txn["id"],
                            "amount": f"{amount:.2f}",
                            "kind": "refund",
                            "gateway": parent_txn["gateway"],
                        }
                    ],
                }
            }
            resp = await client.post(
                f"{self.base_url}/orders/{order_id}/refunds.json",
                headers=self.headers,
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

    @retry(
        retry=retry_if_exception(_is_timeout_or_connection_error),
        before_sleep=_log_retry_attempt,
        **_EXP_BACKOFF,
    )
    async def create_reorder(self, order_id: str, notify_customer: bool = True) -> dict[str, Any]:
        """Creates a new draft order with the same line items as the original, then completes it.
        This is the 'resend' action — used when a customer didn't receive their order and
        a replacement needs to be shipped. ALWAYS call this only after explicit human approval —
        see api/customer_support.py POST /tickets/{id}/actions/resend-order."""
        if not self.enabled:
            raise ShopifyNotConfigured("Shopify credentials not set in .env")

        async with httpx.AsyncClient(timeout=30) as client:
            order_resp = await client.get(
                f"{self.base_url}/orders/{order_id}.json", headers=self.headers
            )
            if order_resp.status_code == 404:
                raise ValueError(f"Order {order_id} not found in Shopify")
            order_resp.raise_for_status()
            original_order = order_resp.json().get("order", {})

            line_items = [
                {"variant_id": li["variant_id"], "quantity": li["quantity"]}
                for li in original_order.get("line_items", [])
                if li.get("variant_id")
            ]
            if not line_items:
                raise ValueError(
                    f"Order {order_id} has no line items with variant IDs — cannot reorder"
                )

            draft_payload = {
                "draft_order": {
                    "line_items": line_items,
                    "note": f"Resend of original order {original_order.get('name', order_id)} — "
                    f"created by AI support agent (human-approved)",
                    "shipping_address": original_order.get("shipping_address"),
                    "email": original_order.get("email"),
                    "customer": {"id": original_order["customer"]["id"]}
                    if original_order.get("customer")
                    else None,
                }
            }
            draft_resp = await client.post(
                f"{self.base_url}/draft_orders.json", headers=self.headers, json=draft_payload
            )
            draft_resp.raise_for_status()
            draft_order = draft_resp.json().get("draft_order", {})
            draft_id = draft_order["id"]

            complete_resp = await client.post(
                f"{self.base_url}/draft_orders/{draft_id}/complete.json",
                headers=self.headers,
                json={"draft_order": {"idempotency_key": f"resend-{order_id}"}},
            )
            complete_resp.raise_for_status()
            new_order = complete_resp.json().get("draft_order", {})

            if notify_customer:
                try:
                    await client.post(
                        f"{self.base_url}/orders/{new_order['id']}/send_receipt.json",
                        headers=self.headers,
                    )
                except Exception:
                    logger.warning(
                        "resend_order_receipt_failed",
                        order_id=order_id,
                        new_order_id=new_order["id"],
                    )

            return {
                "new_order_id": new_order["id"],
                "new_order_name": new_order.get("name"),
                "original_order_id": order_id,
                "original_order_name": original_order.get("name"),
            }

    @staticmethod
    def summarize_order(order: dict[str, Any]) -> str:
        """Turn a raw Shopify order object into a short, LLM-friendly summary."""
        fulfillment_status = order.get("fulfillment_status") or "unfulfilled"
        financial_status = order.get("financial_status", "unknown")
        items = ", ".join(f"{li['quantity']}x {li['title']}" for li in order.get("line_items", []))
        tracking = ""
        for f in order.get("fulfillments", []) or []:
            if f.get("tracking_number"):
                tracking = (
                    f" | Tracking: {f['tracking_number']} ({f.get('tracking_company', 'carrier')})"
                )
                break

        return (
            f"Order {order.get('name')} placed {order.get('created_at')}\n"
            f"Fulfillment status: {fulfillment_status} | Payment status: {financial_status}\n"
            f"Items: {items}\n"
            f"Total: {order.get('total_price')} {order.get('currency')}{tracking}"
        )
