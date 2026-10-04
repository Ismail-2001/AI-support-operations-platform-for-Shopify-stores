"""
Customer Support API Routes.
Endpoints for ticket ingestion (manual + Gorgias webhook), suggestions, responses, analytics.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field, field_validator

from agent.auth import check_shared_secret, verify_api_key
from agent.config import settings
from agent.knowledge_base import knowledge_base
from agent.models import (
    MessageSender,
    SupportAnalytics,
    SupportTicket,
    TicketChannel,
)
from agent.rate_limit import (
    rate_limit_action,
    rate_limit_default,
    rate_limit_refund,
    rate_limit_resend,
)
from agent.storage import storage_is_ephemeral, store
from agent.support_agent import CustomerSupportAgent
from api.errors import (
    APIError,
    raise_not_configured,
    raise_not_found,
    raise_unprocessable,
)
from integrations.gorgias import GorgiasClient, GorgiasNotConfigured
from integrations.shopify import ShopifyNotConfigured

logger = structlog.get_logger(__name__)

# Protected: everything a dashboard/admin/agency operator calls. Requires X-API-Key + rate limited.
router = APIRouter(
    prefix="/support",
    tags=["customer-support"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit_default)],
)

# Public but secret-gated: what Gorgias/Twilio/a chat widget calls. No X-API-Key (those
# services can't easily send one) — each endpoint checks its own shared secret instead.
webhook_router = APIRouter(
    prefix="/support", tags=["webhooks"], dependencies=[Depends(rate_limit_default)]
)

# Fully public: uptime monitors need this to work with no credentials at all.
public_router = APIRouter(prefix="/support", tags=["public"])

_agent = CustomerSupportAgent()
_gorgias = GorgiasClient()


class TicketCreateRequest(BaseModel):
    shop_domain: str | None = None
    customer_email: str
    customer_name: str | None = None
    subject: str
    body: str
    channel: TicketChannel = TicketChannel.EMAIL
    order_id: str | None = None
    order_number: str | None = None
    product_id: str | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("customer_email")
    @classmethod
    def validate_email(cls, v):
        if len(v) > 254:
            raise ValueError("customer_email too long (max 254 chars)")
        if "@" not in v:
            raise ValueError("invalid email format")
        return v.strip()

    @field_validator("body")
    @classmethod
    def validate_body(cls, v):
        if len(v) > 4000:
            raise ValueError("message body too long (max 4000 chars)")
        return v.strip()

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, v):
        if len(v) > 200:
            raise ValueError("subject too long (max 200 chars)")
        return v.strip()

    @field_validator("customer_name")
    @classmethod
    def validate_customer_name(cls, v):
        if v and len(v) > 100:
            raise ValueError("customer_name too long (max 100 chars)")
        return v.strip() if v else v


class TicketUpdateRequest(BaseModel):
    status: str | None = None
    priority: str | None = None
    resolution_notes: str | None = None


class ResponseRequest(BaseModel):
    response: str
    send_via_gorgias: bool = False


# ── Whoami (tenant identity check) ─────────────────────────


@router.get("/whoami")
async def whoami():
    """Return which client's instance this is — run this before any destructive action
    (especially refunds) to confirm you're hitting the right tenant."""
    return {
        "tenant_name": settings.TENANT_NAME,
        "shopify_domain": settings.SHOPIFY_SHOP_DOMAIN,
        "gorgias_domain": settings.GORGIAS_DOMAIN,
    }


# ── Ticket Management ──────────────────────────────────────


@router.post("/tickets")
async def create_ticket(req: TicketCreateRequest):
    """Create a ticket and run it through the full agent pipeline synchronously."""
    ticket_id = f"ticket_{uuid.uuid4().hex[:12]}"
    ticket = SupportTicket(
        id=ticket_id,
        shop_domain=req.shop_domain or settings.SHOPIFY_SHOP_DOMAIN,
        customer_email=req.customer_email,
        customer_name=req.customer_name,
        subject=req.subject,
        body=req.body,
        channel=req.channel,
        order_id=req.order_id,
        order_number=req.order_number,
        product_id=req.product_id,
        metadata=req.metadata or {},
    )

    decision = await _agent.handle_ticket(ticket)

    return {
        "ticket_id": ticket_id,
        "status": "processed",
        "classification": decision.classification.model_dump(),
        "suggestion": decision.suggestion.model_dump(),
        "order_context_used": decision.order_context_used,
        "auto_sent": decision.auto_sent,
    }


@router.get("/tickets")
async def list_tickets(
    status: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    rows = await store.list(
        status=status, category=category, priority=priority, page=page, limit=limit
    )
    total = await store.count(status=status, category=category, priority=priority)
    return {
        "tickets": [
            {**r["ticket"], "suggestion": r["suggestion"], "auto_sent": r["auto_sent"]}
            for r in rows
        ],
        "total": total,
        "page": page,
        "limit": limit,
    }


@router.get("/tickets/{ticket_id}")
async def get_ticket(ticket_id: str):
    row = await store.get(ticket_id)
    if not row:
        raise_not_found("ticket", ticket_id)
    return {**row["ticket"], "suggestion": row["suggestion"], "auto_sent": row["auto_sent"]}


@router.patch("/tickets/{ticket_id}")
async def update_ticket(ticket_id: str, req: TicketUpdateRequest):
    updated = await store.update_status(
        ticket_id, status=req.status, priority=req.priority, resolution_notes=req.resolution_notes
    )
    if not updated:
        raise_not_found("ticket", ticket_id)
    return {"ticket_id": ticket_id, "ticket": updated["ticket"]}


class FollowUpMessageRequest(BaseModel):
    body: str

    @field_validator("body")
    @classmethod
    def validate_body(cls, v):
        if len(v) > 4000:
            raise ValueError("message body too long (max 4000 chars)")
        return v.strip()


@router.post("/tickets/{ticket_id}/messages")
async def add_followup_message(ticket_id: str, req: FollowUpMessageRequest):
    """A new customer message on an existing ticket. Re-runs classification + drafting with
    the full thread as context — this is the endpoint a chat widget or Gorgias message-created
    webhook should call for anything after the first message."""
    decision = await _agent.handle_followup(ticket_id, req.body)
    if not decision:
        raise_not_found("ticket", ticket_id)
    return {
        "ticket_id": ticket_id,
        "classification": decision.classification.model_dump(),
        "suggestion": decision.suggestion.model_dump(),
        "order_context_used": decision.order_context_used,
        "auto_sent": decision.auto_sent,
    }


@router.get("/tickets/{ticket_id}/messages")
async def get_thread(ticket_id: str):
    messages = await store.get_messages(ticket_id)
    if not messages:
        row = await store.get(ticket_id)
        if not row:
            raise_not_found("ticket", ticket_id)
    return {"ticket_id": ticket_id, "messages": [m.model_dump() for m in messages]}


# ── Response Management ────────────────────────────────────


@router.get("/tickets/{ticket_id}/suggestion")
async def get_response_suggestion(ticket_id: str):
    row = await store.get(ticket_id)
    if not row:
        raise_not_found("ticket", ticket_id)
    if not row["suggestion"]:
        raise APIError(
            code="NO_SUGGESTION", message="No suggestion generated for this ticket yet", status=404
        )
    return {"ticket_id": ticket_id, "suggestion": row["suggestion"]}


@router.post("/tickets/{ticket_id}/respond")
async def respond_to_ticket(ticket_id: str, req: ResponseRequest):
    """Human approves (possibly edits) and sends the reply. This is the human-in-the-loop endpoint —
    call it from your agent dashboard's 'Send' button."""
    row = await store.get(ticket_id)
    if not row:
        raise_not_found("ticket", ticket_id)

    gorgias_ticket_id = row["ticket"].get("gorgias_ticket_id")
    if req.send_via_gorgias:
        if not gorgias_ticket_id:
            raise APIError(
                code="NO_GORGIAS_LINK",
                message="Ticket has no linked Gorgias ticket",
                status=400,
            )
        try:
            await _gorgias.post_reply(gorgias_ticket_id, req.response)
        except GorgiasNotConfigured:
            raise_not_configured("Gorgias")

    # Self-improvement tracking: if this ticket had an AI draft, compare it to what was
    # actually sent. High edit rates on a category are the signal to improve that prompt
    # or add more knowledge base content for it — see /support/analytics/quality.
    if row["suggestion"] and row["suggestion"].get("suggested_response"):
        await store.log_edit(
            ticket_id=ticket_id,
            ai_suggestion=row["suggestion"]["suggested_response"],
            final_response=req.response,
            category=row["ticket"].get("category"),
            confidence=row["suggestion"].get("confidence"),
        )

    await store.add_message(ticket_id, MessageSender.AGENT.value, req.response)
    await store.update_status(ticket_id, status="resolved")
    return {
        "ticket_id": ticket_id,
        "status": "response_sent",
        "sent_at": datetime.now(UTC).isoformat(),
    }


# ── Actions (human-approved, money/fulfillment-moving) ──────


class RefundLineItem(BaseModel):
    line_item_id: int
    quantity: int = Field(ge=1)

    @field_validator("line_item_id")
    @classmethod
    def _positive_id(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("line_item_id must be positive")
        return v


class RefundActionRequest(BaseModel):
    amount: float
    reason: str = "Approved by support team"
    notify_customer: bool = True
    # Optional: scope the refund to specific line items (partial / item-level refund).
    refund_line_items: list[RefundLineItem] | None = None


@router.post("/tickets/{ticket_id}/actions/refund", dependencies=[Depends(rate_limit_refund)])
async def approve_refund(
    ticket_id: str,
    req: RefundActionRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    """Executes a REAL refund on the linked Shopify order. This endpoint is never called
    automatically by the agent — auto-send is hard-blocked whenever a suggested_action is
    present (see agent/graph.py decide_auto_send). A human always calls this explicitly,
    e.g. by clicking 'Approve refund' on the AI's suggested_action in your dashboard.

    Requires an `Idempotency-Key` header (e.g. a UUID your dashboard generates once per
    click). If the same key is sent twice — a retried request, a double-click, a network
    retry — the second call returns the FIRST call's result instead of refunding twice.

    The requested amount is capped at the order's total_price MINUS amounts already
    refunded on this order (Shopify's `total_refunded` / `refunds` array) — repeated
    partial refunds can never drain more than the order was worth. Optionally pass
    `refund_line_items` to scope the refund to specific items for partial refunds."""
    existing = await store.get_refund_audit(idempotency_key)
    if existing:
        logger.info(
            "refund_idempotent_replay", idempotency_key=idempotency_key, ticket_id=ticket_id
        )
        return {
            "ticket_id": existing["ticket_id"],
            "order_id": existing["order_id"],
            "refund": existing["shopify_response"],
            "replayed": True,
        }

    row = await store.get(ticket_id)
    if not row:
        raise_not_found("ticket", ticket_id)
    order_id = row["ticket"].get("order_id")
    if not order_id:
        raise APIError(
            code="NO_ORDER_LINKED",
            message="Ticket has no linked order_id — look up the order first",
            status=400,
        )

    if req.amount <= 0:
        raise_unprocessable("Refund amount must be positive")

    try:
        order = await _agent.shopify.get_order_by_id(order_id)
    except ShopifyNotConfigured:
        raise_not_configured("Shopify")

    if not order:
        raise_not_found("order", order_id)

    order_total = float(order.get("total_price", 0))
    try:
        already_refunded = float(order.get("total_refunded") or 0)
    except (TypeError, ValueError):
        already_refunded = 0.0
    if not already_refunded:
        already_refunded = sum(
            float(t.get("amount", 0) or 0)
            for r in order.get("refunds") or []
            for t in r.get("transactions") or []
        )
    refundable = max(order_total - already_refunded, 0.0)
    if req.amount > refundable:
        raise APIError(
            code="REFUND_EXCEEDS_TOTAL",
            message=(
                f"Refund amount {req.amount} exceeds remaining refundable {refundable:.2f} "
                f"(order total {order_total} minus {already_refunded:.2f} already refunded) "
                f"— refusing to process"
            ),
            status=400,
            details={
                "amount": req.amount,
                "order_total": order_total,
                "already_refunded": already_refunded,
                "refundable": refundable,
            },
        )

    shopify_line_items = None
    if req.refund_line_items:
        order_items = {li.get("id"): li for li in order.get("line_items", [])}
        shopify_line_items = []
        for rli in req.refund_line_items:
            li = order_items.get(rli.line_item_id)
            if not li:
                raise APIError(
                    code="REFUND_LINE_ITEM_NOT_FOUND",
                    message=f"Line item {rli.line_item_id} is not on order {order_id}",
                    status=400,
                    details={"line_item_id": rli.line_item_id, "order_id": order_id},
                )
            if rli.quantity > int(li.get("quantity", 0)):
                raise APIError(
                    code="REFUND_QUANTITY_EXCEEDS_ORDER",
                    message=(
                        f"Refund quantity {rli.quantity} exceeds ordered quantity "
                        f"{li.get('quantity')} for {li.get('title', rli.line_item_id)}"
                    ),
                    status=400,
                    details={"line_item_id": rli.line_item_id, "quantity": rli.quantity},
                )
            shopify_line_items.append({"id": rli.line_item_id, "quantity": rli.quantity})

    refund_detail: dict[str, Any] | None = None
    if req.refund_line_items:
        refund_detail = {"line_items": [rli.model_dump() for rli in req.refund_line_items]}

    try:
        refund_kwargs: dict[str, Any] = {
            "order_id": order_id,
            "amount": req.amount,
            "reason": req.reason,
            "notify_customer": req.notify_customer,
        }
        if shopify_line_items is not None:
            refund_kwargs["refund_line_items"] = shopify_line_items
        result = await _agent.shopify.create_refund(**refund_kwargs)
    except Exception as e:
        logger.error("refund_failed", ticket_id=ticket_id, order_id=order_id, error=str(e))
        await store.record_refund_audit(
            idempotency_key,
            ticket_id,
            order_id,
            req.amount,
            req.reason,
            status="failed",
            error=str(e),
            detail=refund_detail,
        )
        raise APIError(code="REFUND_FAILED", message=f"Refund failed: {e}", status=502) from e

    await store.record_refund_audit(
        idempotency_key,
        ticket_id,
        order_id,
        req.amount,
        req.reason,
        status="succeeded",
        shopify_response=result,
        detail=refund_detail,
    )
    await store.add_message(
        ticket_id,
        MessageSender.AGENT.value,
        f"[Action taken] Refund of {req.amount} approved and processed. Reason: {req.reason}",
    )
    await store.update_status(ticket_id, status="resolved")
    logger.info(
        "refund_approved_and_processed", ticket_id=ticket_id, order_id=order_id, amount=req.amount
    )
    return {"ticket_id": ticket_id, "order_id": order_id, "refund": result, "replayed": False}


class ResendOrderActionRequest(BaseModel):
    notify_customer: bool = True
    reason: str = "Replacement order — item not received"


@router.post("/tickets/{ticket_id}/actions/resend-order", dependencies=[Depends(rate_limit_resend)])
async def approve_resend_order(
    ticket_id: str,
    req: ResendOrderActionRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    """Creates a new Shopify order with the same items as the original, then completes it
    so a replacement is shipped. This endpoint is never called automatically — a human
    always calls this explicitly, e.g. by clicking 'Approve resend' on the AI's
    suggested_action in your dashboard.

    Requires an `Idempotency-Key` header. If the same key is sent twice, the second call
    returns the FIRST call's result instead of creating a duplicate order."""
    existing = await store.get_resend_audit(idempotency_key)
    if existing:
        logger.info(
            "resend_idempotent_replay", idempotency_key=idempotency_key, ticket_id=ticket_id
        )
        return {
            "ticket_id": existing["ticket_id"],
            "order_id": existing["order_id"],
            "resend": existing["shopify_response"],
            "replayed": True,
        }

    row = await store.get(ticket_id)
    if not row:
        raise_not_found("ticket", ticket_id)
    order_id = row["ticket"].get("order_id")
    if not order_id:
        raise APIError(
            code="NO_ORDER_LINKED",
            message="Ticket has no linked order_id — look up the order first",
            status=400,
        )

    try:
        order = await _agent.shopify.get_order_by_id(order_id)
    except ShopifyNotConfigured:
        raise_not_configured("Shopify")

    if not order:
        raise_not_found("order", order_id)

    try:
        result = await _agent.shopify.create_reorder(
            order_id=order_id, notify_customer=req.notify_customer
        )
    except Exception as e:
        logger.error("resend_failed", ticket_id=ticket_id, order_id=order_id, error=str(e))
        await store.record_resend_audit(
            idempotency_key, ticket_id, order_id, status="failed", error=str(e)
        )
        raise APIError(code="RESEND_FAILED", message=f"Resend failed: {e}", status=502) from e

    await store.record_resend_audit(
        idempotency_key,
        ticket_id,
        order_id,
        status="succeeded",
        shopify_response=result,
    )
    await store.add_message(
        ticket_id,
        MessageSender.AGENT.value,
        f"[Action taken] Replacement order created. Reason: {req.reason}",
    )
    await store.update_status(ticket_id, status="resolved")
    logger.info("resend_approved_and_processed", ticket_id=ticket_id, order_id=order_id)
    return {"ticket_id": ticket_id, "order_id": order_id, "resend": result, "replayed": False}


@router.get("/tickets/{ticket_id}/order")
async def get_ticket_order(ticket_id: str):
    """The Shopify order linked to this ticket, trimmed to what the dashboard needs for
    approval previews: current shipping address (before an address edit), fulfillment +
    cancellation state (before a cancel), line items + refund history (before a partial
    refund). Read-only — executing any action still requires the /actions/* endpoints."""
    row = await store.get(ticket_id)
    if not row:
        raise_not_found("ticket", ticket_id)
    order_id = row["ticket"].get("order_id")
    if not order_id:
        raise APIError(
            code="NO_ORDER_LINKED",
            message="Ticket has no linked order_id — look up the order first",
            status=400,
        )
    try:
        order = await _agent.shopify.get_order_by_id(order_id)
    except ShopifyNotConfigured:
        raise_not_configured("Shopify")
    if not order:
        raise_not_found("order", order_id)

    already_refunded = float(order.get("total_refunded") or 0)
    if not already_refunded:
        already_refunded = sum(
            float(t.get("amount", 0) or 0)
            for r in order.get("refunds") or []
            for t in r.get("transactions") or []
        )
    order_total = float(order.get("total_price", 0))
    return {
        "ticket_id": ticket_id,
        "order_id": order_id,
        "order_name": order.get("name"),
        "total_price": order_total,
        "currency": order.get("currency"),
        "financial_status": order.get("financial_status"),
        "fulfillment_status": order.get("fulfillment_status"),
        "cancelled_at": order.get("cancelled_at"),
        "shipping_address": order.get("shipping_address") or {},
        "line_items": [
            {
                "id": li.get("id"),
                "title": li.get("title"),
                "variant_title": li.get("variant_title"),
                "quantity": li.get("quantity"),
                "price": li.get("price"),
            }
            for li in order.get("line_items", [])
        ],
        "already_refunded": already_refunded,
        "refundable": max(order_total - already_refunded, 0.0),
    }


class CancelOrderActionRequest(BaseModel):
    reason: str = "Requested by customer"
    notify_customer: bool = True


@router.post("/tickets/{ticket_id}/actions/cancel", dependencies=[Depends(rate_limit_action)])
async def approve_cancel_order(
    ticket_id: str,
    req: CancelOrderActionRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    """Cancels the linked Shopify order (and restocks inventory). Never called automatically —
    a human always calls this explicitly, e.g. by clicking 'Approve cancel' on the AI's
    suggested_action in your dashboard.

    Requires an `Idempotency-Key` header. If the same key is sent twice, the second call
    returns the FIRST call's result instead of cancelling twice.

    Guards: the order must exist, must not already be cancelled, and must not be fulfilled
    (a shipped package is not cancellable — the customer needs a return/refund instead)."""
    existing = await store.get_action_audit(idempotency_key)
    if existing and existing["action"] != "cancel_order":
        raise APIError(
            code="IDEMPOTENCY_KEY_CONFLICT",
            message=f"Idempotency-Key was already used for a different action "
            f"('{existing['action']}') — generate a new key",
            status=409,
        )
    if existing and existing["action"] == "cancel_order":
        logger.info(
            "cancel_idempotent_replay", idempotency_key=idempotency_key, ticket_id=ticket_id
        )
        return {
            "ticket_id": existing["ticket_id"],
            "order_id": existing["order_id"],
            "cancel": existing["shopify_response"],
            "replayed": True,
        }

    row = await store.get(ticket_id)
    if not row:
        raise_not_found("ticket", ticket_id)
    order_id = row["ticket"].get("order_id")
    if not order_id:
        raise APIError(
            code="NO_ORDER_LINKED",
            message="Ticket has no linked order_id — look up the order first",
            status=400,
        )

    try:
        order = await _agent.shopify.get_order_by_id(order_id)
    except ShopifyNotConfigured:
        raise_not_configured("Shopify")

    if not order:
        raise_not_found("order", order_id)

    if order.get("cancelled_at"):
        raise APIError(
            code="ORDER_ALREADY_CANCELLED",
            message=f"Order {order_id} was already cancelled — nothing to do",
            status=409,
            details={"cancelled_at": order["cancelled_at"]},
        )
    fulfillment_status = order.get("fulfillment_status")
    if fulfillment_status and fulfillment_status != "unfulfilled":
        raise APIError(
            code="ORDER_ALREADY_FULFILLED",
            message=(
                f"Order {order_id} is '{fulfillment_status}' — a shipped order cannot be "
                f"cancelled; offer a return or refund instead"
            ),
            status=409,
            details={"fulfillment_status": fulfillment_status},
        )

    try:
        result = await _agent.shopify.cancel_order(
            order_id=order_id, reason=req.reason, notify_customer=req.notify_customer
        )
    except ShopifyNotConfigured:
        raise_not_configured("Shopify")
    except ValueError as e:
        await store.record_action_audit(
            idempotency_key,
            ticket_id,
            order_id,
            "cancel_order",
            req.model_dump(),
            status="failed",
            error=str(e),
        )
        raise APIError(code="ORDER_CANNOT_CANCEL", message=str(e), status=409) from e
    except Exception as e:
        logger.error("cancel_failed", ticket_id=ticket_id, order_id=order_id, error=str(e))
        await store.record_action_audit(
            idempotency_key,
            ticket_id,
            order_id,
            "cancel_order",
            req.model_dump(),
            status="failed",
            error=str(e),
        )
        raise APIError(code="CANCEL_FAILED", message=f"Cancel failed: {e}", status=502) from e

    await store.record_action_audit(
        idempotency_key,
        ticket_id,
        order_id,
        "cancel_order",
        req.model_dump(),
        status="succeeded",
        shopify_response=result,
    )
    await store.add_message(
        ticket_id,
        MessageSender.AGENT.value,
        f"[Action taken] Order cancelled. Reason: {req.reason}",
    )
    await store.update_status(ticket_id, status="resolved")
    logger.info(
        "cancel_approved_and_processed", ticket_id=ticket_id, order_id=order_id, reason=req.reason
    )
    return {"ticket_id": ticket_id, "order_id": order_id, "cancel": result, "replayed": False}


class EditAddressActionRequest(BaseModel):
    address: dict[str, str]
    reason: str = "Customer requested address correction"

    @field_validator("address")
    @classmethod
    def _validate_address(cls, v: dict[str, str]) -> dict[str, str]:
        required = ("address1", "city", "country", "zip")
        missing = [f for f in required if not (v.get(f) or "").strip()]
        if missing:
            raise ValueError(
                f"address is missing required fields: {', '.join(missing)} "
                f"(need address1, city, country, zip)"
            )
        return {k: val.strip() for k, val in v.items() if isinstance(val, str)}


@router.post("/tickets/{ticket_id}/actions/edit-address", dependencies=[Depends(rate_limit_action)])
async def approve_edit_address(
    ticket_id: str,
    req: EditAddressActionRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    """Updates the shipping address on the linked Shopify order. Never called automatically —
    a human always calls this explicitly, e.g. by clicking 'Approve address change' on the
    AI's suggested_action in your dashboard.

    Requires an `Idempotency-Key` header. If the same key is sent twice, the second call
    returns the FIRST call's result instead of editing twice.

    Guards: only unfulfilled/unshipped, non-cancelled orders can be edited — changing the
    address of a package already in transit would silently send it to the wrong place.
    The audit record stores BOTH the previous and the new address for the trail."""
    existing = await store.get_action_audit(idempotency_key)
    if existing and existing["action"] != "edit_address":
        raise APIError(
            code="IDEMPOTENCY_KEY_CONFLICT",
            message=f"Idempotency-Key was already used for a different action "
            f"('{existing['action']}') — generate a new key",
            status=409,
        )
    if existing and existing["action"] == "edit_address":
        logger.info(
            "edit_address_idempotent_replay",
            idempotency_key=idempotency_key,
            ticket_id=ticket_id,
        )
        return {
            "ticket_id": existing["ticket_id"],
            "order_id": existing["order_id"],
            "request": existing["request"],
            "order": existing["shopify_response"],
            "replayed": True,
        }

    row = await store.get(ticket_id)
    if not row:
        raise_not_found("ticket", ticket_id)
    order_id = row["ticket"].get("order_id")
    if not order_id:
        raise APIError(
            code="NO_ORDER_LINKED",
            message="Ticket has no linked order_id — look up the order first",
            status=400,
        )

    try:
        order = await _agent.shopify.get_order_by_id(order_id)
    except ShopifyNotConfigured:
        raise_not_configured("Shopify")

    if not order:
        raise_not_found("order", order_id)

    if order.get("cancelled_at"):
        raise APIError(
            code="ORDER_CANCELLED",
            message=f"Order {order_id} is cancelled — its address cannot be changed",
            status=409,
            details={"cancelled_at": order["cancelled_at"]},
        )
    fulfillment_status = order.get("fulfillment_status")
    if fulfillment_status and fulfillment_status not in ("unfulfilled", "partial"):
        raise APIError(
            code="ORDER_ALREADY_FULFILLED",
            message=(
                f"Order {order_id} is '{fulfillment_status}' — the address can only be "
                f"edited before it ships"
            ),
            status=409,
            details={"fulfillment_status": fulfillment_status},
        )

    previous_address = order.get("shipping_address") or {}
    audit_request = {**req.model_dump(), "previous_address": previous_address}

    try:
        result = await _agent.shopify.update_shipping_address(order_id, req.address)
    except ShopifyNotConfigured:
        raise_not_configured("Shopify")
    except ValueError as e:
        await store.record_action_audit(
            idempotency_key,
            ticket_id,
            order_id,
            "edit_address",
            audit_request,
            status="failed",
            error=str(e),
        )
        raise APIError(code="ADDRESS_UPDATE_REJECTED", message=str(e), status=409) from e
    except Exception as e:
        logger.error("edit_address_failed", ticket_id=ticket_id, order_id=order_id, error=str(e))
        await store.record_action_audit(
            idempotency_key,
            ticket_id,
            order_id,
            "edit_address",
            audit_request,
            status="failed",
            error=str(e),
        )
        raise APIError(
            code="EDIT_ADDRESS_FAILED", message=f"Address update failed: {e}", status=502
        ) from e

    await store.record_action_audit(
        idempotency_key,
        ticket_id,
        order_id,
        "edit_address",
        audit_request,
        status="succeeded",
        shopify_response=result,
    )
    new_line = ", ".join(
        filter(
            None,
            [
                req.address.get("address1"),
                req.address.get("city"),
                req.address.get("zip"),
                req.address.get("country"),
            ],
        )
    )
    old_line = ", ".join(
        filter(
            None,
            [
                previous_address.get("address1"),
                previous_address.get("city"),
                previous_address.get("zip"),
                previous_address.get("country"),
            ],
        )
    )
    await store.add_message(
        ticket_id,
        MessageSender.AGENT.value,
        f"[Action taken] Shipping address updated from [{old_line or 'unknown'}] to "
        f"[{new_line}]. Reason: {req.reason}",
    )
    await store.update_status(ticket_id, status="resolved")
    logger.info("edit_address_approved_and_processed", ticket_id=ticket_id, order_id=order_id)
    return {
        "ticket_id": ticket_id,
        "order_id": order_id,
        "previous_address": previous_address,
        "address": req.address,
        "order": result,
        "replayed": False,
    }


# ── Knowledge Base (RAG) ─────────────────────────────────────


class KBIngestRequest(BaseModel):
    source: str
    title: str
    content: str


@router.post("/knowledge-base")
async def ingest_knowledge(req: KBIngestRequest):
    """Add or replace a knowledge base document (policy page, FAQ, product spec sheet).
    Re-ingesting the same `source` replaces the old chunks instead of duplicating them."""
    await knowledge_base.delete_source(req.source)
    chunk_count = await knowledge_base.ingest(req.source, req.title, req.content)
    return {"source": req.source, "chunks_created": chunk_count}


@router.post("/knowledge-base/sync-shopify")
async def sync_knowledge_from_shopify():
    """Pulls Settings > Policies and active product descriptions from Shopify and ingests
    them as knowledge base content. Run this once after setup and again whenever policies
    or the catalog change meaningfully."""
    if not _agent.shopify.enabled:
        raise_not_configured("Shopify")

    total_chunks = 0
    try:
        policies = await _agent.shopify.get_shop_policies()
        for title, body in policies.items():
            source = f"policy:{title.lower().replace(' ', '-')}"
            await knowledge_base.delete_source(source)
            total_chunks += await knowledge_base.ingest(source, title, body)

        products = await _agent.shopify.get_products(limit=50)
        for p in products:
            source = f"product:{p.get('handle', p.get('id'))}"
            content = f"{p.get('title', '')}\n\n{p.get('body_html', '')}"
            await knowledge_base.delete_source(source)
            total_chunks += await knowledge_base.ingest(
                source, p.get("title", "Untitled product"), content
            )
    except ShopifyNotConfigured:
        raise_not_configured("Shopify")

    return {"status": "synced", "total_chunks": total_chunks}


@router.get("/knowledge-base")
async def knowledge_base_status():
    return {"chunk_count": await knowledge_base.count()}


class KBSearchRequest(BaseModel):
    query: str
    top_k: int = 3


@router.post("/knowledge-base/search")
async def test_knowledge_search(req: KBSearchRequest):
    """Debug endpoint — test what the KB would retrieve for a given customer question,
    without running the full ticket pipeline."""
    results = await knowledge_base.search(req.query, top_k=req.top_k)
    return {"query": req.query, "results": [r.model_dump() for r in results]}


# ── Generic Inbound (non-Gorgias channels: chat widget, WhatsApp via Twilio, etc.) ──


class InboundMessageRequest(BaseModel):
    channel: TicketChannel
    customer_email: str
    customer_name: str | None = None
    subject: str | None = "Chat conversation"
    body: str
    thread_id: str | None = (
        None  # pass the same thread_id on follow-ups from the same customer/session
    )

    @field_validator("customer_email")
    @classmethod
    def validate_email(cls, v):
        if len(v) > 254:
            raise ValueError("customer_email too long (max 254 chars)")
        if "@" not in v:
            raise ValueError("invalid email format")
        return v.strip()

    @field_validator("body")
    @classmethod
    def validate_body(cls, v):
        if len(v) > 4000:
            raise ValueError("message body too long (max 4000 chars)")
        return v.strip()


@webhook_router.post("/webhooks/inbound")
async def generic_inbound_message(
    req: InboundMessageRequest, x_webhook_secret: str | None = Header(None)
):
    """One endpoint for any channel that isn't Gorgias — a website chat widget, a WhatsApp
    number via Twilio/Meta Cloud API, an Instagram DM bridge, etc. Pass the same `thread_id`
    (e.g. the customer's phone number or session id) on every message from the same
    conversation so it's treated as one thread instead of a new ticket each time.

    Set INBOUND_WEBHOOK_SECRET in .env and have whatever's calling this send the same value
    in an X-Webhook-Secret header — this endpoint has no API key, so the secret is the only
    guard against random internet traffic hitting it."""
    check_shared_secret(x_webhook_secret, settings.INBOUND_WEBHOOK_SECRET, "inbound webhook")

    if req.thread_id:
        existing = await store.get(f"inbound_{req.thread_id}")
        if existing:
            decision = await _agent.handle_followup(f"inbound_{req.thread_id}", req.body)
            return {
                "ticket_id": f"inbound_{req.thread_id}",
                "suggestion": decision.suggestion.model_dump() if decision else None,
                "auto_sent": decision.auto_sent if decision else False,
            }

    ticket_id = f"inbound_{uuid.uuid4().hex[:12]}"
    ticket = SupportTicket(
        id=ticket_id,
        customer_email=req.customer_email,
        customer_name=req.customer_name,
        subject=req.subject or "Chat conversation",
        body=req.body,
        channel=req.channel,
    )
    decision = await _agent.handle_ticket(ticket)
    return {
        "ticket_id": ticket_id,
        "suggestion": decision.suggestion.model_dump(),
        "auto_sent": decision.auto_sent,
    }


# ── Gorgias Inbound Webhook ─────────────────────────────────


def _extract_event_id(payload: dict[str, Any]) -> str | None:
    """Extract a unique event id from a Gorgias webhook payload.

    Gorgias places it at the top-level ``id`` for some event types and at
    ``event_data.id`` for others. Return ``None`` when neither is present so
    the caller can still process the request (without dedup protection)."""
    event_id = payload.get("id") or (payload.get("event_data") or {}).get("id")
    if not event_id:
        logger.warning("webhook_missing_event_id", payload_keys=list(payload.keys()))
    return event_id


@webhook_router.post("/webhooks/gorgias/ticket-created")
async def gorgias_ticket_created_webhook(
    request: Request, x_webhook_secret: str | None = Header(None)
):
    """Point Gorgias's 'ticket-created' event webhook at this endpoint.
    Set GORGIAS_WEBHOOK_SECRET and configure the same value as a custom header in Gorgias's
    webhook settings — Gorgias doesn't sign payloads, so a shared secret is the guard here."""
    check_shared_secret(x_webhook_secret, settings.GORGIAS_WEBHOOK_SECRET, "Gorgias webhook")

    payload = await request.json()
    event_id = _extract_event_id(payload)
    if event_id:
        existing = await store.get_processed_webhook_event(event_id, "gorgias")
        if existing:
            logger.info("webhook_duplicate_skipped", event_id=event_id, endpoint="ticket-created")
            return {"received": True, "duplicate": True}

    ticket = GorgiasClient.normalize_webhook_payload(payload)

    decision = await _agent.handle_ticket(ticket)
    await _dispatch_gorgias_reply(ticket.gorgias_ticket_id, decision, ticket.id)

    if event_id:
        await store.record_processed_webhook_event(event_id, "gorgias")

    return {"received": True, "ticket_id": ticket.id, "auto_sent": decision.auto_sent}


@webhook_router.post("/webhooks/gorgias/message-created")
async def gorgias_message_created_webhook(
    request: Request, x_webhook_secret: str | None = Header(None)
):
    """Point Gorgias's 'message-created' event webhook at this endpoint (separate webhook in
    Gorgias's settings from ticket-created). Handles follow-up customer messages on tickets we've
    already seen — appends to the same thread instead of creating a duplicate ticket."""
    check_shared_secret(x_webhook_secret, settings.GORGIAS_WEBHOOK_SECRET, "Gorgias webhook")

    payload = await request.json()
    event_id = _extract_event_id(payload)
    if event_id:
        existing = await store.get_processed_webhook_event(event_id, "gorgias")
        if existing:
            logger.info("webhook_duplicate_skipped", event_id=event_id, endpoint="message-created")
            return {"received": True, "duplicate": True}

    raw = payload.get("event_data", payload)
    gorgias_ticket_id = str(raw.get("ticket_id") or raw.get("ticket", {}).get("id", ""))
    message_body = raw.get("body_text") or raw.get("stripped_text") or ""

    if not gorgias_ticket_id or not message_body:
        return {"received": True, "skipped": "missing ticket_id or message body"}

    # Only react to customer messages — ignore webhook echoes of our own agent replies.
    if raw.get("from_agent") is True:
        return {"received": True, "skipped": "message was from an agent, not the customer"}

    ticket = await store.get_ticket_by_gorgias_id(gorgias_ticket_id)
    if not ticket:
        logger.warning("gorgias_followup_unknown_ticket", gorgias_ticket_id=gorgias_ticket_id)
        return {"received": True, "skipped": "no matching ticket on file for this Gorgias ticket"}

    decision = await _agent.handle_followup(ticket.id, message_body)
    await _dispatch_gorgias_reply(gorgias_ticket_id, decision, ticket.id)

    if event_id:
        await store.record_processed_webhook_event(event_id, "gorgias")

    return {
        "received": True,
        "ticket_id": ticket.id,
        "auto_sent": decision.auto_sent if decision else False,
    }


async def _dispatch_gorgias_reply(gorgias_ticket_id: str | None, decision, ticket_id: str) -> None:
    """High-confidence + policy-clear -> send straight back to the customer via Gorgias.
    Everything else -> attach as an internal note so a human sees the draft in Gorgias itself.

    On failure: logs the error and does NOT silently swallow — the caller must know the
    reply was not delivered so the ticket status reflects reality."""
    if not (decision and _gorgias.enabled and gorgias_ticket_id):
        return
    try:
        if decision.auto_sent:
            await _gorgias.post_reply(gorgias_ticket_id, decision.suggestion.suggested_response)
        else:
            note = (
                f"AI draft (confidence {decision.suggestion.confidence:.2f}, "
                f"review required):\n\n{decision.suggestion.suggested_response}"
            )
            await _gorgias.add_internal_note(gorgias_ticket_id, note)
    except GorgiasNotConfigured:
        logger.warning("gorgias_not_configured", ticket_id=ticket_id)
    except Exception as e:
        logger.error(
            "gorgias_reply_failed",
            ticket_id=ticket_id,
            gorgias_ticket_id=gorgias_ticket_id,
            error=str(e),
        )
        if decision.auto_sent:
            # The reply was supposed to go to the customer but Gorgias failed.
            # Flip auto_sent to False so the ticket doesn't falsely claim delivery.
            await store.update_status(ticket_id, auto_sent=False)


# ── Analytics ──────────────────────────────────────────────


@router.get("/analytics", response_model=SupportAnalytics)
async def get_support_analytics(days: int = Query(7, ge=1, le=90)):
    agg = await store.analytics()
    total = agg["total"]
    return SupportAnalytics(
        total_tickets=total,
        open_tickets=agg["open"],
        first_contact_resolution_rate=(agg["auto_sent"] / total) if total else None,
        category_breakdown=agg["category_breakdown"],
        priority_breakdown=agg["priority_breakdown"],
        channel_breakdown=agg["channel_breakdown"],
        sentiment_distribution=agg["sentiment_distribution"],
    )


# ── Health ─────────────────────────────────────────────────


@router.get("/analytics/quality")
async def get_quality_analytics():
    """Self-improvement signal: how often humans edit the AI's drafts before sending, broken
    down by category. A category with a high edit_rate is telling you exactly where to spend
    your next hour — better prompt instructions, more knowledge base content, or leave that
    category as human-only for now."""
    return await store.get_edit_stats()


@router.get("/analytics/calibration")
async def get_calibration_report():
    """Confidence calibration: is the model's self-reported confidence actually trustworthy?
    Buckets past AI drafts by confidence and shows edit rate per bucket. If edit_rate doesn't
    drop as confidence rises, AUTO_SEND_MIN_CONFIDENCE is not doing what you think it's doing —
    raise it, or don't trust auto-send for that category yet."""
    return await store.get_calibration_report()


@router.get("/analytics/auto-send")
async def get_auto_send_analytics():
    """Per-category auto-send thresholds: the current floor, the floor recommended from real
    human edit behavior (50+ reviewed samples per category), and why. Blocked categories
    (refund/complaint/legal/other) are listed separately — they never auto-send, threshold
    or not. Apply a suggestion via PUT /support/automation/thresholds."""
    from agent.automation import get_auto_send_report

    return await get_auto_send_report()


class ThresholdUpdateRequest(BaseModel):
    category: str
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


@router.get("/automation/thresholds")
async def list_automation_thresholds():
    """Current auto-send confidence floor per category, plus where each value came from
    (runtime override vs .env). These are read on every ticket — changes apply immediately,
    no restart."""
    from agent.automation import get_thresholds

    return {"thresholds": await get_thresholds()}


@router.put("/automation/thresholds")
async def update_automation_threshold(req: ThresholdUpdateRequest):
    """Set (or clear, with min_confidence=null) the runtime auto-send confidence floor for
    one category. Only auto-sendable categories accept thresholds — refund/complaint/legal/
    other are hard-blocked in code regardless of any value set here. A daily cost cap stays
    active independently: thresholds never disable it."""
    from agent.automation import (
        AUTO_SENDABLE_CATEGORIES,
        _kv_delete,
        set_min_confidence,
        threshold_kv_key,
    )

    if req.category not in AUTO_SENDABLE_CATEGORIES:
        raise_unprocessable(
            f"'{req.category}' is not auto-sendable — thresholds only apply to: "
            f"{', '.join(AUTO_SENDABLE_CATEGORIES)}"
        )
    if req.min_confidence is None:
        await _kv_delete(threshold_kv_key(req.category))
        logger.info("threshold_override_cleared", category=req.category)
        source = "settings"
        effective = None
    else:
        try:
            await set_min_confidence(req.category, req.min_confidence)
        except ValueError as e:
            raise_unprocessable(str(e))
        source = "runtime_override"
        effective = req.min_confidence

    from agent.automation import get_thresholds

    for entry in await get_thresholds():
        if entry["category"] == req.category:
            return {
                "category": req.category,
                "min_confidence": entry["min_confidence"],
                "source": source if effective is not None else entry["source"],
            }
    raise_unprocessable(f"unknown category '{req.category}'")


@router.get("/analytics/costs")
async def get_cost_analytics(days: int = Query(7, ge=1, le=90)):
    """Real LLM spend, not an estimate — pulled from the llm_costs table populated on every
    classification/response call. today_usd is what DAILY_COST_CAP_USD is compared against."""
    return await store.get_cost_report(days=days)


# ── Observability (debugging "why did it say that") ──────────


@router.get("/tickets/{ticket_id}/trace")
async def get_ticket_trace(ticket_id: str):
    """Full pipeline trace for one ticket: every LLM call's exact input (transcript, order
    context, KB context) and output (classification/suggestion), latency, tokens, and cost —
    in call order. This is what you pull up when a client asks 'why did the bot say that?'"""
    row = await store.get(ticket_id)
    if not row:
        raise_not_found("ticket", ticket_id)
    traces = await store.get_traces(ticket_id)
    return {"ticket_id": ticket_id, "trace_count": len(traces), "traces": traces}


# ── Health ─────────────────────────────────────────────────


@public_router.get("/health")
async def customer_support_health():
    return {
        "status": "healthy",
        "shopify_connected": _agent.shopify.enabled,
        "gorgias_connected": _gorgias.enabled,
        "auto_send_enabled": settings.AUTO_SEND_ENABLED,
        "storage_persistent": not storage_is_ephemeral(),
        "timestamp": datetime.now(UTC).isoformat(),
    }
