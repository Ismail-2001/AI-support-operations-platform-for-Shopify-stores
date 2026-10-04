"""Minimal Shopify Admin API client — orders, catalog (paginated), metafields."""

import re
from typing import Any
from urllib.parse import unquote

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

_PAGE_INFO_RE = re.compile(r"page_info=([^&>]+)")


def _parse_page_info(link_header: str | None) -> str | None:
    """Extract the `page_info` cursor of rel="next" from Shopify's Link header.

    Pure function so pagination is unit-testable without HTTP. Returns None when
    there's no next page (single page, or end of catalog)."""
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            match = _PAGE_INFO_RE.search(part)
            if match:
                return unquote(match.group(1))
    return None


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
    def __init__(self, shop_domain: str | None = None, access_token: str | None = None):
        # Overrides let a per-store agent instance target a different shop while
        # the process default keeps coming from settings (see agent/storage.py stores).
        domain = shop_domain or settings.SHOPIFY_SHOP_DOMAIN
        token = access_token or settings.SHOPIFY_ACCESS_TOKEN
        self.enabled = bool(domain and token)
        if self.enabled:
            self.base_url = f"https://{domain}/admin/api/{settings.SHOPIFY_API_VERSION}"
            secret = token.get_secret_value() if hasattr(token, "get_secret_value") else str(token)
            self.headers = {
                "X-Shopify-Access-Token": secret,
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
        """Active products, following Shopify's Link cursor so catalogs larger than
        one page (250 max) sync completely instead of silently truncating at 50."""
        if not self.enabled:
            raise ShopifyNotConfigured("Shopify credentials not set in .env")
        limit = max(limit, 1)
        page_size = min(limit, 250)
        products: list[dict[str, Any]] = []
        page_info: str | None = None
        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                params: dict[str, Any] = {"limit": page_size, "status": "active"}
                if page_info:
                    params["page_info"] = page_info
                resp = await client.get(
                    f"{self.base_url}/products.json", headers=self.headers, params=params
                )
                resp.raise_for_status()
                page = resp.json().get("products", [])
                products.extend(page)
                if len(products) >= limit or not page:
                    break
                page_info = _parse_page_info(resp.headers.get("Link"))
                if not page_info:
                    break
        return products[:limit]

    @retry(
        retry=retry_if_exception(_is_transient_shopify_error),
        before_sleep=_log_retry_attempt,
        **_EXP_BACKOFF,
    )
    async def get_product_metafields(self, product_id: int | str) -> list[dict[str, Any]]:
        """Public/stored metafields for a product (care instructions, materials,
        sizing guides, warranty live here on most stores).

        Missing/forbidden metafields return [] instead of failing the whole sync —
        a store without metafield permissions must still index everything else."""
        if not self.enabled:
            raise ShopifyNotConfigured("Shopify credentials not set in .env")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/products/{product_id}/metafields.json",
                headers=self.headers,
                params={"limit": 50},
            )
            if resp.status_code in (403, 404):
                logger.info("shopify_metafields_unavailable", product_id=product_id)
                return []
            resp.raise_for_status()
            return resp.json().get("metafields", [])

    @retry(
        retry=retry_if_exception(_is_transient_shopify_error),
        before_sleep=_log_retry_attempt,
        **_EXP_BACKOFF,
    )
    async def get_product_by_handle(self, handle: str) -> dict[str, Any] | None:
        """Live product fetch by handle — used for real-time inventory checks in the
        dashboard's Test Product Knowledge tool (KB chunks carry sync-time stock)."""
        if not self.enabled:
            raise ShopifyNotConfigured("Shopify credentials not set in .env")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/products.json",
                headers=self.headers,
                params={"handle": handle, "limit": 1},
            )
            resp.raise_for_status()
            products = resp.json().get("products", [])
            return products[0] if products else None

    @retry(
        retry=retry_if_exception(_is_timeout_or_connection_error),
        before_sleep=_log_retry_attempt,
        **_EXP_BACKOFF,
    )
    async def create_refund(
        self,
        order_id: str,
        amount: float,
        reason: str = "",
        notify_customer: bool = True,
        refund_line_items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Creates a monetary refund on an order. ALWAYS call this only after explicit human
        approval — see api/customer_support.py POST /tickets/{id}/actions/refund. This method
        itself does not gate on anything; the safety gate lives at the API layer.

        `refund_line_items` optionally scopes the refund to specific line items
        ([{"id": <line_item id>, "quantity": 2}]) for partial / item-level refunds.
        When omitted, the full amount is refunded as a plain monetary refund."""
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
            if refund_line_items:
                payload["refund"]["refund_line_items"] = refund_line_items
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
    async def cancel_order(
        self, order_id: str, reason: str = "", notify_customer: bool = True
    ) -> dict[str, Any]:
        """Cancels an unfulfilled order (and restocks its inventory). ALWAYS call this only
        after explicit human approval — see api/customer_support.py POST /tickets/{id}/actions/cancel.
        Strict retry (timeout/connection only): a 5xx may mean Shopify already processed the
        cancellation, so we never blindly retry — same policy as create_refund."""
        if not self.enabled:
            raise ShopifyNotConfigured("Shopify credentials not set in .env")

        async with httpx.AsyncClient(timeout=20) as client:
            # Shopify's cancel reason is a fixed set — map free-text human reasons
            # to the closest valid value (our own audit table keeps the exact reason).
            valid_reasons = {"customer", "fraud", "inventory", "declined", "other"}
            payload = {
                "cancel": {
                    "reason": reason if reason in valid_reasons else "customer",
                    "notify": notify_customer,
                    "restock": True,
                }
            }
            resp = await client.post(
                f"{self.base_url}/orders/{order_id}/cancel.json",
                headers=self.headers,
                json=payload,
            )
            if resp.status_code == 404:
                raise ValueError(f"Order {order_id} not found in Shopify")
            if resp.status_code == 422:
                # Shopify rejects cancelling an already-cancelled order — surface the
                # reason so the API layer can translate it into a clear 409.
                detail = resp.json().get("errors", resp.text)
                raise ValueError(f"Order {order_id} cannot be cancelled: {detail}")
            resp.raise_for_status()
            return resp.json()

    @retry(
        retry=retry_if_exception(_is_timeout_or_connection_error),
        before_sleep=_log_retry_attempt,
        **_EXP_BACKOFF,
    )
    async def update_shipping_address(
        self, order_id: str, address: dict[str, Any]
    ) -> dict[str, Any]:
        """Updates the shipping address on an UNFULFILLED order. ALWAYS call this only after
        explicit human approval — see api/customer_support.py POST /tickets/{id}/actions/edit-address.
        Refuses to touch orders that are already fulfilled or cancelled — editing an address
        after the package shipped would silently send it to the wrong place."""
        if not self.enabled:
            raise ShopifyNotConfigured("Shopify credentials not set in .env")

        async with httpx.AsyncClient(timeout=20) as client:
            order_resp = await client.get(
                f"{self.base_url}/orders/{order_id}.json", headers=self.headers
            )
            if order_resp.status_code == 404:
                raise ValueError(f"Order {order_id} not found in Shopify")
            order_resp.raise_for_status()
            order = order_resp.json().get("order", {})

            if order.get("cancelled_at"):
                raise ValueError(f"Order {order_id} is cancelled — address cannot be changed")
            fulfillment_status = order.get("fulfillment_status")
            if fulfillment_status and fulfillment_status not in ("unfulfilled", "partial"):
                raise ValueError(
                    f"Order {order_id} is '{fulfillment_status}' — address can only be edited "
                    f"before it ships"
                )

            resp = await client.put(
                f"{self.base_url}/orders/{order_id}.json",
                headers=self.headers,
                json={"order": {"id": order_id, "shipping_address": address}},
            )
            if resp.status_code == 422:
                detail = resp.json().get("errors", resp.text)
                raise ValueError(f"Shopify rejected the address update: {detail}")
            resp.raise_for_status()
            return resp.json()

    @retry(
        retry=retry_if_exception(_is_transient_shopify_error),
        before_sleep=_log_retry_attempt,
        **_EXP_BACKOFF,
    )
    async def tag_order(self, order_id: str, tag: str) -> dict[str, Any]:
        """Add a tag to an order (idempotent — returns the order untouched when the
        tag is already there). Used by best-effort annotations like
        'return-label-created'; callers must never fail an action because of this."""
        if not self.enabled:
            raise ShopifyNotConfigured("Shopify credentials not set in .env")

        async with httpx.AsyncClient(timeout=20) as client:
            order_resp = await client.get(
                f"{self.base_url}/orders/{order_id}.json", headers=self.headers
            )
            if order_resp.status_code == 404:
                raise ValueError(f"Order {order_id} not found in Shopify")
            order_resp.raise_for_status()
            order = order_resp.json().get("order", {})
            existing = [t.strip() for t in str(order.get("tags") or "").split(",") if t.strip()]
            if tag in existing:
                return order

            resp = await client.put(
                f"{self.base_url}/orders/{order_id}.json",
                headers=self.headers,
                json={"order": {"id": order_id, "tags": ", ".join([*existing, tag])}},
            )
            resp.raise_for_status()
            return resp.json().get("order", {})

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
