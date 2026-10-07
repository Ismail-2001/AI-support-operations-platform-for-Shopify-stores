"""
Customer Support API Routes.
Endpoints for ticket ingestion (manual + Gorgias webhook), suggestions, responses, analytics.
"""

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from agent.alerting import send_alert
from agent.auth import check_shared_secret, verify_api_key
from agent.config import settings
from agent.context_proxy import ContextProxy
from agent.knowledge_base import knowledge_base
from agent.models import (
    MessageSender,
    SubscriptionOperation,
    SupportAnalytics,
    SupportTicket,
    TicketChannel,
)
from agent.product_knowledge import stock_snapshot
from agent.product_sync import get_sync_status, run_sync, start_sync
from agent.rate_limit import (
    rate_limit_action,
    rate_limit_default,
    rate_limit_refund,
    rate_limit_resend,
)
from agent.resilience import CircuitOpenError
from agent.returns import evaluate_return_eligibility
from agent.roi import compute_roi, get_roi_settings, save_roi_settings
from agent.storage import storage_is_ephemeral, store
from agent.support_agent import CustomerSupportAgent
from api.errors import (
    APIError,
    raise_not_configured,
    raise_not_found,
    raise_unprocessable,
)
from integrations.gorgias import GorgiasClient, GorgiasNotConfigured
from integrations.shipengine import ShipEngineClient
from integrations.shopify import ShopifyNotConfigured
from integrations.subscriptions import (
    SubscriptionError,
    SubscriptionService,
)

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


def _current_support_agent() -> CustomerSupportAgent | None:
    from agent.multistore import agent_for_current

    return agent_for_current()


# Requests carrying X-Store-Id resolve to that store's agent (own Shopify
# credentials); every other context uses this deployment-wide default.
_agent = ContextProxy(CustomerSupportAgent(), _current_support_agent)
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
    """Return which client's instance this is - run this before any destructive action
    (especially refunds) to confirm you're hitting the right tenant. Inside an
    X-Store-Id scope the answer is that STORE's identity, not the deployment's."""
    from agent.multistore import get_store, get_store_id

    store_id = get_store_id()
    if store_id:
        rec = await get_store(store_id)
        return {
            "store_id": store_id,
            "tenant_name": rec["name"] if rec else store_id,
            "shopify_domain": rec["shop_domain"] if rec else "",
            "gorgias_domain": settings.GORGIAS_DOMAIN,
        }
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

    gorgias_ticket_id = row["ticket"].get("gorgias_ticket_id") or (
        (row["ticket"].get("metadata") or {}).get("gorgias_ticket_id")
    )
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
        except CircuitOpenError:
            # The global handler maps this to 503 CIRCUIT_OPEN — a known, retryable
            # state, not a surprise failure worth paging anyone about.
            raise
        except Exception as exc:
            await send_alert(
                title="Reply failed to send",
                message=f"Ticket {ticket_id}: {type(exc).__name__}: {exc}",
                severity="error",
                dedupe_key="respond_failed",
            )
            raise APIError(
                code="REPLY_FAILED",
                message=f"Gorgias send failed: {exc}",
                status=502,
            ) from exc

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


async def _reject_cross_family_key(idempotency_key: str, own_table: str) -> None:
    """Reject an Idempotency-Key already spent in a DIFFERENT action family.

    Families: refund_audit, resend_audit, action_audit (cancel / edit-address /
    subscription), return_label_audit. Within a family each endpoint keeps its
    own replay + conflict rules; across families the key is a client bug and we
    return 409 rather than silently executing a different action."""
    family = await store.find_idempotency_family(idempotency_key)
    if family and family != own_table:
        raise APIError(
            code="IDEMPOTENCY_KEY_CONFLICT",
            message=(
                f"Idempotency-Key was already used for a different action "
                f"(table '{family}') - generate a new key"
            ),
            status=409,
            details={"table": family},
        )


def _replay_audit(
    existing: dict[str, Any], build_success: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    """Replay the outcome recorded for an already-used Idempotency-Key.

    - succeeded → the original 200 payload from `build_success`
    - failed → the ORIGINAL error status + code. A failed money action must never
      replay as HTTP 200 — the side effect never happened, and the client needs
      the real status to decide whether to retry with a fresh key.
    - in_progress → 409: a concurrent request with this key still holds the claim
      and has not finished executing."""
    status = existing.get("status")
    if status == "in_progress":
        raise APIError(
            code="ACTION_IN_PROGRESS",
            message=(
                "A request with this Idempotency-Key is still being processed — "
                "wait a moment and retry to receive its result"
            ),
            status=409,
            details={"idempotency_key": existing.get("idempotency_key")},
        )
    if status == "failed":
        raise APIError(
            code=existing.get("error_code") or "ACTION_FAILED",
            message=existing.get("error")
            or "The original request with this Idempotency-Key failed",
            status=int(existing.get("http_status") or 502),
            details={"replayed": True},
        )
    return build_success()


def _claim_lost(
    existing: dict[str, Any] | None, build_success: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    """Handle losing the atomic claim race: another request already owns the key.
    Re-read its row and replay whatever outcome it recorded (or is executing)."""
    if existing is None:  # pragma: no cover - the row cannot vanish mid-request
        raise APIError(
            code="IDEMPOTENCY_CONTENTION",
            message="Idempotency-Key contention — retry, generating a new key",
            status=409,
        )
    return _replay_audit(existing, build_success)


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

    def _refund_replay(existing: dict[str, Any]) -> dict[str, Any]:
        return {
            "ticket_id": existing["ticket_id"],
            "order_id": existing["order_id"],
            "refund": existing["shopify_response"],
            "replayed": True,
        }

    existing = await store.get_refund_audit(idempotency_key)
    if existing:
        logger.info(
            "refund_idempotent_replay", idempotency_key=idempotency_key, ticket_id=ticket_id
        )
        return _replay_audit(existing, lambda: _refund_replay(existing))
    await _reject_cross_family_key(idempotency_key, "refund_audit")

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

    # All validation passed — atomically claim the key BEFORE money moves, so a
    # concurrent request with the same key can never reach create_refund twice.
    if not await store.claim_refund_audit(
        idempotency_key, ticket_id, order_id, req.amount, req.reason
    ):
        logger.info("refund_idempotent_race", idempotency_key=idempotency_key, ticket_id=ticket_id)
        owner = await store.get_refund_audit(idempotency_key)
        return _claim_lost(owner, lambda: _refund_replay(owner))

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
            http_status=502,
            error_code="REFUND_FAILED",
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

    def _resend_replay(existing: dict[str, Any]) -> dict[str, Any]:
        return {
            "ticket_id": existing["ticket_id"],
            "order_id": existing["order_id"],
            "resend": existing["shopify_response"],
            "replayed": True,
        }

    existing = await store.get_resend_audit(idempotency_key)
    if existing:
        logger.info(
            "resend_idempotent_replay", idempotency_key=idempotency_key, ticket_id=ticket_id
        )
        return _replay_audit(existing, lambda: _resend_replay(existing))
    await _reject_cross_family_key(idempotency_key, "resend_audit")

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

    # Claim the key before creating a duplicate order — a concurrent replay must
    # never reach create_reorder twice.
    if not await store.claim_resend_audit(idempotency_key, ticket_id, order_id):
        logger.info("resend_idempotent_race", idempotency_key=idempotency_key, ticket_id=ticket_id)
        owner = await store.get_resend_audit(idempotency_key)
        return _claim_lost(owner, lambda: _resend_replay(owner))

    try:
        result = await _agent.shopify.create_reorder(
            order_id=order_id, notify_customer=req.notify_customer
        )
    except Exception as e:
        logger.error("resend_failed", ticket_id=ticket_id, order_id=order_id, error=str(e))
        await store.record_resend_audit(
            idempotency_key,
            ticket_id,
            order_id,
            status="failed",
            error=str(e),
            http_status=502,
            error_code="RESEND_FAILED",
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

    def _cancel_replay(existing: dict[str, Any]) -> dict[str, Any]:
        return {
            "ticket_id": existing["ticket_id"],
            "order_id": existing["order_id"],
            "cancel": existing["shopify_response"],
            "replayed": True,
        }

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
        return _replay_audit(existing, lambda: _cancel_replay(existing))
    await _reject_cross_family_key(idempotency_key, "action_audit")

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

    # All state guards passed — claim the key before cancelling (inventory restock
    # + customer notification are not reversible).
    if not await store.claim_action_audit(
        idempotency_key, ticket_id, order_id, "cancel_order", req.model_dump()
    ):
        logger.info("cancel_idempotent_race", idempotency_key=idempotency_key, ticket_id=ticket_id)
        owner = await store.get_action_audit(idempotency_key)
        if owner is not None and owner.get("action") != "cancel_order":
            raise APIError(
                code="IDEMPOTENCY_KEY_CONFLICT",
                message=f"Idempotency-Key was already used for a different action "
                f"('{owner['action']}') — generate a new key",
                status=409,
            )
        return _claim_lost(owner, lambda: _cancel_replay(owner))

    try:
        result = await _agent.shopify.cancel_order(
            order_id=order_id, reason=req.reason, notify_customer=req.notify_customer
        )
    except ShopifyNotConfigured:
        await store.record_action_audit(
            idempotency_key,
            ticket_id,
            order_id,
            "cancel_order",
            req.model_dump(),
            status="failed",
            error="Shopify is not configured",
            http_status=400,
            error_code="SHOPIFY_NOT_CONFIGURED",
        )
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
            http_status=409,
            error_code="ORDER_CANNOT_CANCEL",
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
            http_status=502,
            error_code="CANCEL_FAILED",
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

    def _edit_replay(existing: dict[str, Any]) -> dict[str, Any]:
        return {
            "ticket_id": existing["ticket_id"],
            "order_id": existing["order_id"],
            "request": existing["request"],
            "order": existing["shopify_response"],
            "replayed": True,
        }

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
        return _replay_audit(existing, lambda: _edit_replay(existing))
    await _reject_cross_family_key(idempotency_key, "action_audit")

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

    # State guards passed — claim the key before the address actually changes.
    if not await store.claim_action_audit(
        idempotency_key, ticket_id, order_id, "edit_address", audit_request
    ):
        logger.info(
            "edit_address_idempotent_race",
            idempotency_key=idempotency_key,
            ticket_id=ticket_id,
        )
        owner = await store.get_action_audit(idempotency_key)
        if owner is not None and owner.get("action") != "edit_address":
            raise APIError(
                code="IDEMPOTENCY_KEY_CONFLICT",
                message=f"Idempotency-Key was already used for a different action "
                f"('{owner['action']}') — generate a new key",
                status=409,
            )
        return _claim_lost(owner, lambda: _edit_replay(owner))

    try:
        result = await _agent.shopify.update_shipping_address(order_id, req.address)
    except ShopifyNotConfigured:
        await store.record_action_audit(
            idempotency_key,
            ticket_id,
            order_id,
            "edit_address",
            audit_request,
            status="failed",
            error="Shopify is not configured",
            http_status=400,
            error_code="SHOPIFY_NOT_CONFIGURED",
        )
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
            http_status=409,
            error_code="ADDRESS_UPDATE_REJECTED",
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
            http_status=502,
            error_code="EDIT_ADDRESS_FAILED",
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


# ── Subscriptions (Recharge / Skio) ────────────────────────


def _subscription_service(provider: str | None) -> SubscriptionService:
    if provider and provider not in ("recharge", "skio"):
        raise_unprocessable(
            f"Unknown subscription provider '{provider}' — use 'recharge' or 'skio'"
        )
    return SubscriptionService(provider=provider)


async def _ticket_email(ticket_id: str, email_override: str | None) -> str:
    if email_override:
        return email_override.strip()
    row = await store.get(ticket_id)
    if not row:
        raise_not_found("ticket", ticket_id)
    email = row["ticket"].get("customer_email")
    if not email:
        raise APIError(
            code="NO_CUSTOMER_EMAIL",
            message="Ticket has no customer_email — pass ?email= to look up subscriptions",
            status=400,
        )
    return str(email)


@router.get("/tickets/{ticket_id}/subscriptions")
async def get_ticket_subscriptions(ticket_id: str, email: str | None = Query(None)):
    """List the ticket customer's subscriptions from the connected provider.

    Returns 200 with `configured: false` (rather than 409) when no provider is
    connected so the dashboard can render a 'connect an app' state."""
    customer_email = await _ticket_email(ticket_id, email)
    service = SubscriptionService()
    if not service.enabled:
        return {
            "ticket_id": ticket_id,
            "email": customer_email,
            "configured": False,
            "provider": None,
            "subscriptions": [],
        }
    try:
        subs = await service.list_subscriptions(customer_email)
    except SubscriptionError as e:
        raise APIError(code=e.code, message=str(e), status=e.status, details=e.details) from e
    return {
        "ticket_id": ticket_id,
        "email": customer_email,
        "configured": True,
        "provider": service.provider,
        "subscriptions": [s.model_dump(exclude={"raw"}) for s in subs],
    }


class SubscriptionActionRequest(BaseModel):
    subscription_id: str
    operation: SubscriptionOperation
    provider: str | None = None  # "recharge" | "skio"; defaults to the configured provider
    reason: str = "Requested by customer"
    address: dict[str, str] | None = None
    frequency: dict[str, Any] | None = None  # {"unit": "week", "count": 2}

    @field_validator("subscription_id")
    @classmethod
    def _validate_subscription_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("subscription_id is required")
        return v

    @field_validator("address")
    @classmethod
    def _validate_address(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return v
        required = ("address1", "city", "country", "zip")
        missing = [f for f in required if not (v.get(f) or "").strip()]
        if missing:
            raise ValueError(f"address is missing required fields: {', '.join(missing)}")
        return {k: val.strip() for k, val in v.items() if isinstance(val, str)}


@router.post("/tickets/{ticket_id}/actions/subscription", dependencies=[Depends(rate_limit_action)])
async def approve_subscription_action(
    ticket_id: str,
    req: SubscriptionActionRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    """Executes an approved subscription operation (pause / skip / cancel /
    update_address / change_frequency) against Recharge or Skio. Never called
    automatically — a human always approves it from the dashboard.

    Requires an `Idempotency-Key` header: the same key returns the FIRST call's
    result instead of executing the operation twice (skipping twice or cancelling
    twice are both customer-visible mistakes).

    Guards (shared across providers, enforced in SubscriptionService):
    - subscription must exist and not already be cancelled
    - skip requires an upcoming charge
    - change_frequency accepts only day/week/month with count 1-60"""
    audit_action = f"subscription_{req.operation.value}"

    def _subscription_replay(existing: dict[str, Any]) -> dict[str, Any]:
        return {
            "ticket_id": existing["ticket_id"],
            "subscription": (existing.get("shopify_response") or {}).get("subscription"),
            "operation": req.operation.value,
            "status": existing["status"],
            "error": existing.get("error"),
            "replayed": True,
        }

    existing = await store.get_action_audit(idempotency_key)
    if existing and existing["action"] != audit_action:
        raise APIError(
            code="IDEMPOTENCY_KEY_CONFLICT",
            message=f"Idempotency-Key was already used for a different action "
            f"('{existing['action']}') — generate a new key",
            status=409,
        )
    if existing and existing["action"] == audit_action:
        logger.info(
            "subscription_action_idempotent_replay",
            idempotency_key=idempotency_key,
            ticket_id=ticket_id,
            operation=req.operation.value,
        )
        return _replay_audit(existing, lambda: _subscription_replay(existing))
    await _reject_cross_family_key(idempotency_key, "action_audit")

    row = await store.get(ticket_id)
    if not row:
        raise_not_found("ticket", ticket_id)

    audit_request = req.model_dump(mode="json")
    service = _subscription_service(req.provider)

    # Claim the key before the provider call (skip twice / cancel twice are both
    # customer-visible mistakes a replay must never repeat).
    if not await store.claim_action_audit(
        idempotency_key, ticket_id, "", audit_action, audit_request
    ):
        logger.info(
            "subscription_action_idempotent_race",
            idempotency_key=idempotency_key,
            ticket_id=ticket_id,
            operation=req.operation.value,
        )
        owner = await store.get_action_audit(idempotency_key)
        if owner is not None and owner.get("action") != audit_action:
            raise APIError(
                code="IDEMPOTENCY_KEY_CONFLICT",
                message=f"Idempotency-Key was already used for a different action "
                f"('{owner['action']}') — generate a new key",
                status=409,
            )
        return _claim_lost(owner, lambda: _subscription_replay(owner))

    try:
        if req.operation == SubscriptionOperation.PAUSE:
            after = await service.pause(req.subscription_id)
        elif req.operation == SubscriptionOperation.SKIP:
            after = await service.skip(req.subscription_id)
        elif req.operation == SubscriptionOperation.CANCEL:
            after = await service.cancel(req.subscription_id, reason=req.reason)
        elif req.operation == SubscriptionOperation.UPDATE_ADDRESS:
            if not req.address:
                raise_unprocessable("update_address requires an `address` object")
            after = await service.update_address(req.subscription_id, req.address)
        elif req.operation == SubscriptionOperation.CHANGE_FREQUENCY:
            freq = req.frequency or {}
            unit = freq.get("unit")
            count = freq.get("count")
            if not unit or count is None:
                raise_unprocessable(
                    'change_frequency requires frequency = {"unit": "day"|"week"|"month", "count": N}'
                )
            try:
                after = await service.change_frequency(req.subscription_id, str(unit), int(count))
            except (TypeError, ValueError) as e:
                raise_unprocessable(f"Invalid frequency: {e}")
        else:  # pragma: no cover - enum covers all branches above
            raise_unprocessable(f"Unsupported operation '{req.operation}'")
    except SubscriptionError as e:
        # Not-configured (409), not-found (404), invalid-state guards (409) and
        # provider failures (502) are all auditable outcomes, not 500s — record
        # the failed attempt, then map to the error class's documented code.
        await store.record_action_audit(
            idempotency_key,
            ticket_id,
            order_id="",
            action=audit_action,
            request=audit_request,
            status="failed",
            error=str(e),
            http_status=e.status,
            error_code=e.code,
        )
        raise APIError(code=e.code, message=str(e), status=e.status, details=e.details) from e
    except APIError as e:
        # Validation raised after the claim (address/frequency shape, unsupported
        # op): record it so the key replays as the same 4xx instead of going
        # stale 'in_progress' forever.
        await store.record_action_audit(
            idempotency_key,
            ticket_id,
            order_id="",
            action=audit_action,
            request=audit_request,
            status="failed",
            error=e.detail if isinstance(e.detail, str) else json.dumps(e.detail),
            http_status=e.status_code,
            error_code=e.error_code,
        )
        raise
    except Exception as e:
        # Unexpected failure: finalize the claim so the key can never go stale
        # 'in_progress' (which would 409 forever).
        await store.record_action_audit(
            idempotency_key,
            ticket_id,
            order_id="",
            action=audit_action,
            request=audit_request,
            status="failed",
            error=str(e),
            http_status=500,
            error_code="SUBSCRIPTION_ACTION_FAILED",
        )
        raise

    response_payload = {
        "subscription": after.model_dump(exclude={"raw"}),
        "provider": service.provider,
    }
    await store.record_action_audit(
        idempotency_key,
        ticket_id,
        # Subscriptions aren't order-scoped; keep the column non-null per schema.
        order_id="",
        action=audit_action,
        request=audit_request,
        status="succeeded",
        shopify_response=response_payload,
    )

    verb = {
        SubscriptionOperation.PAUSE: "paused",
        SubscriptionOperation.SKIP: "next order skipped",
        SubscriptionOperation.CANCEL: f"cancelled (reason: {req.reason})",
        SubscriptionOperation.UPDATE_ADDRESS: "shipping address updated",
        SubscriptionOperation.CHANGE_FREQUENCY: (
            f"frequency changed to every {req.frequency.get('count')} "
            f"{req.frequency.get('unit')}"
            if req.frequency
            else "frequency changed"
        ),
    }[req.operation]
    await store.add_message(
        ticket_id,
        MessageSender.AGENT.value,
        f"[Action taken] Subscription {req.subscription_id}: {verb}.",
    )
    logger.info(
        "subscription_action_approved",
        ticket_id=ticket_id,
        subscription_id=req.subscription_id,
        operation=req.operation.value,
        provider=service.provider,
    )
    return {
        "ticket_id": ticket_id,
        **response_payload,
        "operation": req.operation.value,
        "replayed": False,
    }


# ── Return labels (ShipEngine) ─────────────────────────────


def _store_return_window_days() -> int | None:
    """Per-store RETURN_WINDOW_DAYS override (agency mode), else process default."""
    from agent.returns import current_return_window_days

    return current_return_window_days()


def _return_customer_address(order: dict) -> dict[str, str]:
    """Ship-from address for a prepaid return: the customer, taken from the
    order's shipping address (billing as fallback)."""
    shipping = order.get("shipping_address") or order.get("billing_address") or {}
    return {
        "name": " ".join(filter(None, [shipping.get("first_name"), shipping.get("last_name")]))
        or (order.get("customer") or {}).get("first_name")
        or "Customer",
        "address1": shipping.get("address1"),
        "address2": shipping.get("address2"),
        "city": shipping.get("city"),
        "state": shipping.get("province") or shipping.get("state"),
        "zip": shipping.get("zip"),
        "country": shipping.get("country"),
        "phone": shipping.get("phone"),
    }


def _require_return_address(order: dict) -> dict[str, str]:
    address = _return_customer_address(order)
    if not (address.get("address1") and address.get("city")):
        raise APIError(
            code="NO_SHIPPING_ADDRESS",
            message="Order has no shipping address — cannot build a return shipment",
            status=409,
        )
    return address


@router.get("/tickets/{ticket_id}/return-eligibility")
async def get_return_eligibility(ticket_id: str):
    """Whether a prepaid return label can be created for this ticket's order,
    plus what's blocking it when it can't (window expired, not shipped, missing
    ShipEngine config...). The approval endpoint re-checks all of this server-side."""
    row = await store.get(ticket_id)
    if not row:
        raise_not_found("ticket", ticket_id)

    shipengine = ShipEngineClient()
    configured, missing_fields = shipengine.configured

    order_id = row["ticket"].get("order_id")
    order = None
    shopify_error = None
    if order_id:
        try:
            order = await _agent.shopify.get_order_by_id(order_id)
        except ShopifyNotConfigured:
            shopify_error = "Shopify is not configured — cannot look up the order"
        if order is None and shopify_error is None:
            shopify_error = f"Order '{order_id}' not found"

    eligibility = evaluate_return_eligibility(order, window_days=_store_return_window_days())
    if shopify_error:
        eligibility = {**eligibility, "eligible": False, "reason": shopify_error}

    return {
        "ticket_id": ticket_id,
        "order_id": order_id,
        **eligibility,
        "label_provider": {
            "provider": "shipengine",
            "configured": configured,
            "missing_settings": missing_fields,
        },
    }


@router.get("/tickets/{ticket_id}/return-rates")
async def get_return_rates(ticket_id: str):
    """Live carrier rates for this ticket's prepaid return — the cost the human
    approves BEFORE any money moves. Re-validates eligibility + configuration
    server-side; the dashboard then purchases the chosen rate via
    POST .../actions/return-label with `rate_id`."""
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

    shipengine = ShipEngineClient()
    if not shipengine.enabled:
        raise_not_configured("ShipEngine")
    configured, missing_fields = shipengine.configured
    if not configured:
        raise APIError(
            code="RETURN_ADDRESS_MISSING",
            message=f"Return address settings incomplete: {', '.join(missing_fields)}",
            status=409,
            details={"missing_settings": missing_fields},
        )

    try:
        order = await _agent.shopify.get_order_by_id(order_id)
    except ShopifyNotConfigured:
        raise_not_configured("Shopify")
    if not order:
        raise_not_found("order", order_id)

    eligibility = evaluate_return_eligibility(order, window_days=_store_return_window_days())
    if not eligibility["eligible"]:
        raise APIError(
            code="RETURN_NOT_ELIGIBLE",
            message=str(eligibility["reason"]),
            status=409,
            details={"reason": eligibility["reason"], "window_days": eligibility["window_days"]},
        )

    address = _require_return_address(order)
    rates = await shipengine.get_rates(address)
    if not rates:
        raise APIError(
            code="NO_RATES_AVAILABLE",
            message="No carrier rates available for this return shipment",
            status=422,
        )
    out = []
    for r in rates:
        ship_rate = r.get("ship_rate") or {}
        out.append(
            {
                "rate_id": r.get("rate_id"),
                "carrier": r.get("carrier_friendly_name") or r.get("carrier_id"),
                "service": r.get("service_type") or r.get("service_code"),
                "cost_usd": float(ship_rate.get("amount") or 0),
                "currency": ship_rate.get("currency") or "USD",
            }
        )
    return {
        "ticket_id": ticket_id,
        "order_id": order_id,
        "rates": out,
        "cheapest_rate_id": out[0]["rate_id"],
    }


class ReturnLabelActionRequest(BaseModel):
    rate_id: str | None = None  # pre-approved rate; cheapest is chosen when omitted
    rma_number: str | None = None
    reason: str = "Return label requested"


@router.post("/tickets/{ticket_id}/actions/return-label", dependencies=[Depends(rate_limit_action)])
async def approve_return_label(
    ticket_id: str,
    req: ReturnLabelActionRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    """Purchases a real prepaid return label (ShipEngine) for the ticket's order.
    Never called automatically — a human approves it from the dashboard, because
    the label costs money.

    Requires an `Idempotency-Key` header: replaying the key returns the FIRST
    result instead of buying a second paid label. Eligibility (return window,
    shipped/refunded state) is re-verified server-side here — the dashboard's
    eligibility preview is advisory only."""

    def _label_replay(existing: dict[str, Any]) -> dict[str, Any]:
        return {
            "ticket_id": existing["ticket_id"],
            "order_id": existing["order_id"],
            "label": {
                "label_id": existing["label_id"],
                "label_url": existing["label_url"],
                "tracking_number": existing["tracking_number"],
                "carrier_service": existing["carrier_service"],
                "cost_usd": existing["cost_usd"],
            },
            "error": existing["error"],
            "replayed": True,
        }

    existing = await store.get_return_label_audit(idempotency_key)
    if existing:
        logger.info(
            "return_label_idempotent_replay",
            idempotency_key=idempotency_key,
            ticket_id=ticket_id,
            status=existing["status"],
        )
        return _replay_audit(existing, lambda: _label_replay(existing))
    await _reject_cross_family_key(idempotency_key, "return_label_audit")

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

    shipengine = ShipEngineClient()
    if not shipengine.enabled:
        raise_not_configured("ShipEngine")
    configured, missing_fields = shipengine.configured
    if not configured:
        raise APIError(
            code="RETURN_ADDRESS_MISSING",
            message=f"Return address settings incomplete: {', '.join(missing_fields)}",
            status=409,
            details={"missing_settings": missing_fields},
        )

    try:
        order = await _agent.shopify.get_order_by_id(order_id)
    except ShopifyNotConfigured:
        raise_not_configured("Shopify")
    if not order:
        raise_not_found("order", order_id)

    eligibility = evaluate_return_eligibility(order, window_days=_store_return_window_days())
    if not eligibility["eligible"]:
        raise APIError(
            code="RETURN_NOT_ELIGIBLE",
            message=str(eligibility["reason"]),
            status=409,
            details={"reason": eligibility["reason"], "window_days": eligibility["window_days"]},
        )

    # Prepaid return: FROM the customer (order shipping address) TO the store.
    customer_address = _require_return_address(order)

    rma = req.rma_number or f"T-{ticket_id}"
    audit_request = req.model_dump(mode="json")

    # Eligibility passed — claim the key before paying for a label.
    if not await store.claim_return_label_audit(
        idempotency_key, ticket_id, order_id, audit_request
    ):
        logger.info(
            "return_label_idempotent_race",
            idempotency_key=idempotency_key,
            ticket_id=ticket_id,
        )
        owner = await store.get_return_label_audit(idempotency_key)
        return _claim_lost(owner, lambda: _label_replay(owner))

    try:
        label = await shipengine.quote_and_buy(
            customer_address, rma_number=rma, rate_id=req.rate_id
        )
    except APIError as e:
        await store.record_return_label_audit(
            idempotency_key,
            ticket_id,
            order_id,
            status="failed",
            provider="shipengine",
            request=audit_request,
            error=str(e.detail),
            http_status=e.status_code,
            error_code=e.error_code,
        )
        raise
    except Exception as e:
        logger.error("return_label_failed", ticket_id=ticket_id, order_id=order_id, error=str(e))
        await store.record_return_label_audit(
            idempotency_key,
            ticket_id,
            order_id,
            status="failed",
            provider="shipengine",
            request=audit_request,
            error=str(e),
            http_status=502,
            error_code="RETURN_LABEL_FAILED",
        )
        raise APIError(
            code="RETURN_LABEL_FAILED", message=f"Return label failed: {e}", status=502
        ) from e

    await store.record_return_label_audit(
        idempotency_key,
        ticket_id,
        order_id,
        status="succeeded",
        provider="shipengine",
        label_id=label["label_id"],
        label_url=label["label_url"],
        tracking_number=label["tracking_number"],
        carrier_service=label.get("carrier") or label.get("service_code"),
        cost_usd=label["cost_usd"],
        request=audit_request,
    )
    await store.add_message(
        ticket_id,
        MessageSender.AGENT.value,
        f"[Action taken] Return label created. Tracking: {label['tracking_number']} — "
        f"attach it to the package and hand it to the carrier. "
        f"Label PDF: {label['label_url']}",
    )
    # The ball is now in the customer's court (they must ship it back).
    await store.update_status(ticket_id, status="awaiting_customer")

    # Best-effort Shopify tag so the merchant can filter return-ship orders.
    # Goes through the ticket's (possibly store-scoped) Shopify client; a fake or
    # missing tag_order just logs and moves on — never fail the label over a tag.
    tagger = getattr(_agent.shopify, "tag_order", None)
    if tagger:
        try:
            await tagger(order_id, "return-label-created")
        except Exception as e:
            logger.warning("return_label_tag_failed", order_id=order_id, error=str(e))

    logger.info(
        "return_label_approved_and_processed",
        ticket_id=ticket_id,
        order_id=order_id,
        label_id=label["label_id"],
        cost_usd=label["cost_usd"],
    )
    return {
        "ticket_id": ticket_id,
        "order_id": order_id,
        "label": label,
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
async def sync_knowledge_from_shopify(
    background_tasks: BackgroundTasks, force: bool = Query(False)
):
    """Starts an incremental Shopify → KB sync in the background (policies + full
    active catalog with variants/metafields/alt-text). Returns immediately; poll
    GET /support/knowledge-base/sync-status for progress. Unchanged documents are
    hash-skipped, so re-running is cheap. `?force=true` re-embeds everything."""
    if not _agent.shopify.enabled:
        raise_not_configured("Shopify")
    if not start_sync(force=force):
        return {"status": "running"}
    # Capture the scope now: the background task must keep operating on THIS
    # store's KB/status even if it runs after the request context is reset.
    from agent.multistore import get_store_id

    background_tasks.add_task(run_sync, force, get_store_id())
    return {"status": "started", "force": force}


@router.get("/knowledge-base/sync-status")
async def knowledge_sync_status():
    """Progress of the background catalog sync (started/running/finished + counters)."""
    return get_sync_status()


@router.get("/knowledge-base/live-stock")
async def live_product_stock(handle: str = Query(..., min_length=1, max_length=200)):
    """Real-time inventory for a product handle — the KB chunk carries stock as of
    the last sync; this answers "is it actually in stock right now." """
    if not _agent.shopify.enabled:
        raise_not_configured("Shopify")
    product = await _agent.shopify.get_product_by_handle(handle)
    if not product:
        raise_not_found("product", handle)
    return {
        "handle": product.get("handle"),
        "title": product.get("title"),
        "variants": stock_snapshot(product),
        "checked_at": datetime.now(UTC).isoformat(),
    }


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


async def _record_dead_letter(
    source: str, payload: dict[str, Any], exc: Exception, store_id: str | None = None
) -> None:
    """Persist a failed webhook payload for operator redrive and push an alert.

    Never raises: if the DLQ write itself fails we still owe the caller a 2xx
    (a 5xx would make Gorgias retry-storm us — exactly what the DLQ prevents)."""
    from agent.multistore import get_store_id

    scope = store_id if store_id is not None else get_store_id()
    error = f"{type(exc).__name__}: {exc}"
    try:
        dl_id = await store.record_dead_letter(
            source=source, payload=payload, error=error, store_id=scope
        )
        logger.error(
            "webhook_dead_letter_recorded",
            source=source,
            dead_letter_id=dl_id,
            store_id=scope,
            error=error,
        )
    except Exception as record_exc:
        logger.error("webhook_dead_letter_record_failed", source=source, error=str(record_exc))
    await send_alert(
        title=f"Webhook failed: {source}",
        message=(
            f"{error}\n\nPayload preserved — inspect via GET /support/dead-letters "
            "and redrive via POST /support/dead-letters/{id}/retry."
        ),
        severity="error",
        dedupe_key=f"dead_letter:{source}",
    )


async def _process_inbound(req: InboundMessageRequest) -> dict[str, Any]:
    """Process one inbound-channel message. Split out of the webhook handler so
    the dead-letter redrive endpoint can replay a failed payload verbatim."""
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
    guard against random internet traffic hitting it.

    Processing failures return 200 with the payload preserved in the dead-letter
    queue (GET /support/dead-letters) rather than a 5xx: the channel would just
    retry-storm us, and the operator gets an alert instead."""
    check_shared_secret(x_webhook_secret, settings.INBOUND_WEBHOOK_SECRET, "inbound webhook")

    try:
        return await _process_inbound(req)
    except Exception as exc:
        await _record_dead_letter("inbound", req.model_dump(mode="json"), exc)
        return JSONResponse(
            status_code=200,
            content={"received": True, "queued": True, "error": str(exc)[:300]},
        )


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


def _gorgias_webhook_secret() -> str | None:
    """Webhook shared secret for the active scope.

    Store scope: ONLY that store's own secret (a tenant that hasn't set one
    gets the standard 'not configured' hard-fail in production - never another
    tenant's secret). Default scope: GORGIAS_WEBHOOK_SECRET from .env."""
    from agent.multistore import integration_overrides

    overrides = integration_overrides()
    if overrides is not None:
        return overrides.get("gorgias_webhook_secret") or None
    return settings.GORGIAS_WEBHOOK_SECRET


async def _process_gorgias_ticket_created(payload: dict[str, Any]) -> dict[str, Any]:
    """Dedupe, normalize, classify, dispatch. Split out of the webhook handler so
    the dead-letter redrive endpoint can replay a failed payload verbatim."""
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


@webhook_router.post("/webhooks/gorgias/ticket-created")
@webhook_router.post("/webhooks/gorgias/{store_id}/ticket-created")
async def gorgias_ticket_created_webhook(
    request: Request, x_webhook_secret: str | None = Header(None), store_id: str | None = None
):
    """Point Gorgias's 'ticket-created' event webhook at this endpoint.
    Set GORGIAS_WEBHOOK_SECRET and configure the same value as a custom header in Gorgias's
    webhook settings — Gorgias doesn't sign payloads, so a shared secret is the guard here.

    Multi-store: each tenant configures /support/webhooks/gorgias/<store_id>/ticket-created
    (the middleware scopes the request to that store; `store_id` here is informational).

    Processing failures return 200 with the payload preserved in the dead-letter
    queue (GET /support/dead-letters) rather than a 5xx — see _record_dead_letter."""
    check_shared_secret(x_webhook_secret, _gorgias_webhook_secret(), "Gorgias webhook")

    payload = await request.json()
    try:
        return await _process_gorgias_ticket_created(payload)
    except Exception as exc:
        await _record_dead_letter("gorgias:ticket-created", payload, exc, store_id=store_id)
        return JSONResponse(
            status_code=200,
            content={"received": True, "queued": True, "error": str(exc)[:300]},
        )


async def _process_gorgias_message_created(payload: dict[str, Any]) -> dict[str, Any]:
    """Follow-up path for tickets we've already seen. Split out of the webhook
    handler so the dead-letter redrive endpoint can replay a failed payload."""
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


@webhook_router.post("/webhooks/gorgias/message-created")
@webhook_router.post("/webhooks/gorgias/{store_id}/message-created")
async def gorgias_message_created_webhook(
    request: Request, x_webhook_secret: str | None = Header(None), store_id: str | None = None
):
    """Point Gorgias's 'message-created' event webhook at this endpoint (separate webhook in
    Gorgias's settings from ticket-created). Handles follow-up customer messages on tickets we've
    already seen — appends to the same thread instead of creating a duplicate ticket.
    Multi-store variant: /support/webhooks/gorgias/<store_id>/message-created.
    Failures go to the dead-letter queue with a 200 — see _record_dead_letter."""
    check_shared_secret(x_webhook_secret, _gorgias_webhook_secret(), "Gorgias webhook")

    payload = await request.json()
    try:
        return await _process_gorgias_message_created(payload)
    except Exception as exc:
        await _record_dead_letter("gorgias:message-created", payload, exc, store_id=store_id)
        return JSONResponse(
            status_code=200,
            content={"received": True, "queued": True, "error": str(exc)[:300]},
        )


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
        await send_alert(
            title="Gorgias dispatch failed",
            message=(
                f"Ticket {ticket_id}: {type(e).__name__}: {e} — "
                + (
                    "auto-sent reply was NOT delivered to the customer."
                    if decision.auto_sent
                    else "internal note was NOT attached."
                )
            ),
            severity="error",
            dedupe_key="gorgias_dispatch_failed",
        )
        if decision.auto_sent:
            # The reply was supposed to go to the customer but Gorgias failed.
            # Flip auto_sent to False so the ticket doesn't falsely claim delivery.
            await store.update_status(ticket_id, auto_sent=False)


# ── Dead letters: inspect + redrive failed webhook payloads ──

# Sources the redrive endpoint knows how to replay. Functions are referenced by
# name (not evaluated) so the processors above can be defined in any order.
_REPLAY_SOURCES: dict[str, Any] = {
    "gorgias:ticket-created": _process_gorgias_ticket_created,
    "gorgias:message-created": _process_gorgias_message_created,
    "inbound": _process_inbound,
}


@router.get("/dead-letters")
async def list_dead_letters(
    status: str | None = Query(None, pattern="^(pending|retried|failed)$"),
    limit: int = Query(50, ge=1, le=200),
):
    """Webhook payloads that failed processing, newest first. `pending` rows are
    un-retried; each redrive marks the row `retried` (success) or `failed` (another
    failure, attempts counter bumped). Store-scoped requests list that store's queue."""
    rows = await store.list_dead_letters(status=status, limit=limit)
    return {
        "dead_letters": rows,
        "pending": await store.count_pending_dead_letters(),
    }


@router.post("/dead-letters/{dead_letter_id}/retry")
async def retry_dead_letter(dead_letter_id: int):
    """Replay a dead-lettered payload through the same processor the webhook uses.

    Runs in the store scope the payload failed in (per the row's store_id), so the
    ticket lands in the right tenant DB. Failures mark the row `failed` and return
    502 REDRIVE_FAILED — the payload stays available for another attempt."""
    row = await store.get_dead_letter(dead_letter_id)
    if not row:
        raise_not_found("dead_letter", dead_letter_id)
    if row["status"] == "retried":
        raise APIError(
            code="ALREADY_RETRIED",
            message=f"Dead letter {dead_letter_id} was already redriven successfully",
            status=409,
        )
    handler = _REPLAY_SOURCES.get(row["source"])
    if handler is None:
        raise APIError(
            code="UNSUPPORTED_SOURCE",
            message=f"No replayer registered for source {row['source']!r}",
            status=422,
        )

    payload = json.loads(row["payload"])

    # Enter the store scope this payload failed in, if it differs from the current one.
    from agent.multistore import StoreNotFound, current_store_id, ensure_store_ready, get_store_id

    token = None
    scope = row.get("store_id")
    if scope and scope != get_store_id():
        try:
            await ensure_store_ready(scope)
        except StoreNotFound:
            raise APIError(
                code="STORE_GONE",
                message=(
                    f"Dead letter belongs to store {scope!r}, which no longer exists — "
                    "it cannot be replayed in its original scope"
                ),
                status=409,
            ) from None
        token = current_store_id.set(scope)

    try:
        if row["source"] == "inbound":
            result = await handler(InboundMessageRequest(**payload))
        else:
            result = await handler(payload)
        await store.mark_dead_letter(dead_letter_id, "retried")
        return {"dead_letter_id": dead_letter_id, "status": "retried", "result": result}
    except Exception as exc:
        await store.mark_dead_letter(dead_letter_id, "failed", error=f"{type(exc).__name__}: {exc}")
        raise APIError(
            code="REDRIVE_FAILED",
            message=f"Redrive failed: {type(exc).__name__}: {exc}",
            status=502,
        ) from exc
    finally:
        if token is not None:
            current_store_id.reset(token)


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


@router.get("/analytics/pilot")
async def get_pilot_report(days: str = Query("7", pattern=r"^(all|[1-9]\d{0,3})$")):
    """The numbers a pilot review runs on, in one call: auto-resolve %, human edit
    rate, time-to-first-response (mean + median), and LLM spend for the window.
    `days` = 7 | 30 | a number of days | all. ROI estimates (owner-adjustable
    assumptions) live at /support/analytics/roi; per-category breakdowns at
    /support/analytics/quality."""
    since = None if days == "all" else (datetime.now(UTC) - timedelta(days=int(days))).isoformat()
    metrics = await store.get_pilot_metrics(since)
    metrics["window_days"] = days
    return metrics


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


class RoiSettingsRequest(BaseModel):
    """The store owner's own assumptions — visible, editable, and stored in KV.
    These drive every estimated number on the ROI page; measured numbers (LLM
    cost, ticket counts) never depend on them."""

    minutes_per_auto_sent: float = Field(10, gt=0, le=240)
    minutes_per_draft: float = Field(5, gt=0, le=240)
    hourly_rate_usd: float = Field(25, gt=0, le=1000)


@router.get("/analytics/roi")
async def get_roi_report(days: str = Query("7", pattern=r"^(all|[1-9]\d{0,3})$")):
    """ROI/impact report: measured LLM spend vs estimated human-time savings.
    `days` = 7 | 30 | a number of days | all. Estimates use the assumptions from
    PUT /support/analytics/roi/settings (defaults: 10 min/auto-sent, 5 min/draft,
    $25/hour)."""
    since = None if days == "all" else (datetime.now(UTC) - timedelta(days=int(days))).isoformat()
    raw = await store.get_roi_aggregates(since)
    report = compute_roi(raw, await get_roi_settings())
    report["days"] = days
    report["since"] = since
    return report


@router.put("/analytics/roi/settings")
async def update_roi_settings(req: RoiSettingsRequest):
    """Update the ROI assumptions and return the effective values."""
    return {"assumptions": await save_roi_settings(req.model_dump())}


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


# ── Storefront chat widget settings ───────────────────────────


@router.get("/widget")
async def get_widget_settings():
    """Publishable key + appearance config for the embeddable chat widget.
    The key is safe to expose (it ships in the storefront's <script> tag) — it only
    gates chat sessions; this endpoint itself stays behind the private API key."""
    from agent.widget_store import get_widget_config, get_widget_key

    return {"key": await get_widget_key(), "config": await get_widget_config()}


class WidgetConfigUpdate(BaseModel):
    enabled: bool | None = None
    title: str | None = Field(default=None, max_length=80)
    greeting: str | None = Field(default=None, max_length=200)
    welcome_message: str | None = Field(default=None, max_length=400)
    color: str | None = Field(default=None, max_length=7)
    logo_url: str | None = Field(default=None, max_length=500)
    show_confidence: bool | None = None


@router.put("/widget")
async def update_widget_settings(req: WidgetConfigUpdate):
    """Update widget appearance/behavior at runtime — applies to the live widget
    immediately (it re-fetches config on load), no redeploy."""
    from agent.widget_store import set_widget_config

    try:
        config = await set_widget_config(req.model_dump(exclude_none=True))
    except ValueError as e:
        raise_unprocessable(str(e))
    return {"config": config}


@router.post("/widget/key")
async def rotate_widget_key_endpoint():
    """Rotate the publishable widget key. Old key stops working on the next request;
    re-copy the install snippet into the storefront. Refuses when WIDGET_KEY is set
    in .env (env wins — rotate it there instead)."""
    from agent.widget_store import rotate_widget_key, widget_key_is_env_managed

    if widget_key_is_env_managed():
        raise APIError(
            code="WIDGET_KEY_ENV_MANAGED",
            message="WIDGET_KEY is set in .env — rotate it there, then restart.",
            status=409,
        )
    key = await rotate_widget_key()
    return {"key": key}


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
    from agent.resilience import circuit_snapshots

    return {
        "status": "healthy",
        "shopify_connected": _agent.shopify.enabled,
        "gorgias_connected": _gorgias.enabled,
        "auto_send_enabled": settings.AUTO_SEND_ENABLED,
        "storage_persistent": not storage_is_ephemeral(),
        "circuits": circuit_snapshots(),
        "timestamp": datetime.now(UTC).isoformat(),
    }


# --- Multi-store registry (agency packaging) ---
# Registry reads/writes always hit the primary DB and ignore any X-Store-Id
# context on the request — they are global admin operations.


class StoreCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    shop_domain: str = ""
    shopify_access_token: str | None = None
    # Per-store integration settings (all optional; secrets are write-only).
    shipengine_api_key: str | None = None
    recharge_api_token: str | None = None
    skio_api_token: str | None = None
    subscription_provider: str | None = None  # "auto" | "recharge" | "skio"
    return_address: dict[str, str] | None = None
    return_window_days: int | None = Field(default=None, ge=0, le=365)  # 0 = use global
    # Per-store Gorgias helpdesk (agency tenants each bring their own desk).
    gorgias_domain: str | None = None
    gorgias_email: str | None = None
    gorgias_api_key: str | None = None
    gorgias_webhook_secret: str | None = None

    @field_validator("shop_domain")
    @classmethod
    def _normalize_domain(cls, v: str) -> str:
        return (v or "").strip().lower()

    @field_validator("subscription_provider")
    @classmethod
    def _validate_provider(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        v = v.strip().lower()
        if v not in ("auto", "recharge", "skio"):
            raise ValueError("subscription_provider must be auto, recharge, or skio")
        return v

    @field_validator("return_address")
    @classmethod
    def _validate_address(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        if not v:
            return v  # {} = explicit "clear" marker, distinct from omitted (None)
        cleaned = {k: str(val).strip() for k, val in v.items() if isinstance(val, str)}
        required = ("name", "address1", "city", "zip")
        missing = [f for f in required if not cleaned.get(f)]
        if missing:
            raise ValueError(f"return_address is missing required fields: {', '.join(missing)}")
        return cleaned


class StoreUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    shop_domain: str | None = None
    shopify_access_token: str | None = None
    # Per-store integration settings: None = unchanged, "" / {} / 0 = clear
    # (fall back to process env / global default). Secrets are never echoed back.
    shipengine_api_key: str | None = None
    recharge_api_token: str | None = None
    skio_api_token: str | None = None
    subscription_provider: str | None = None
    return_address: dict[str, str] | None = None
    return_window_days: int | None = Field(default=None, ge=0, le=365)
    gorgias_domain: str | None = None
    gorgias_email: str | None = None
    gorgias_api_key: str | None = None
    gorgias_webhook_secret: str | None = None

    @field_validator("shop_domain")
    @classmethod
    def _normalize_domain(cls, v: str | None) -> str | None:
        return v.strip().lower() if v is not None else None

    @field_validator("subscription_provider")
    @classmethod
    def _validate_provider(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        v = v.strip().lower()
        if v not in ("auto", "recharge", "skio"):
            raise ValueError("subscription_provider must be auto, recharge, or skio")
        return v

    @field_validator("return_address")
    @classmethod
    def _validate_address(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        if not v:
            return v  # {} = explicit "clear" marker, distinct from omitted (None)
        cleaned = {k: str(val).strip() for k, val in v.items() if isinstance(val, str)}
        required = ("name", "address1", "city", "zip")
        missing = [f for f in required if not cleaned.get(f)]
        if missing:
            raise ValueError(f"return_address is missing required fields: {', '.join(missing)}")
        return cleaned


async def _find_duplicate_shop_domain(shop_domain: str, exclude_id: str | None = None) -> bool:
    if not shop_domain:
        return False
    from agent.multistore import list_stores

    for rec in await list_stores():
        if rec["shop_domain"] == shop_domain and rec["id"] != exclude_id:
            return True
    return False


def _integration_credentials(
    req: StoreCreateRequest | StoreUpdateRequest,
) -> dict[str, str | int | dict | None]:
    """Merge patch for per-store integration settings. None on a field = leave
    unchanged; "" / {} / 0 = clear (fall back to process env / global default)."""
    out: dict[str, str | int | dict | None] = {}
    for field in (
        "shipengine_api_key",
        "recharge_api_token",
        "skio_api_token",
        "gorgias_domain",
        "gorgias_email",
        "gorgias_api_key",
        "gorgias_webhook_secret",
    ):
        value = getattr(req, field)
        if value is not None:
            out[field] = value.strip() or None
    if req.subscription_provider is not None:
        out["subscription_provider"] = req.subscription_provider or None
    if req.return_address is not None:
        out["return_address"] = req.return_address or None
    if req.return_window_days is not None:
        out["return_window_days"] = req.return_window_days or None
    return out


@router.get("/stores")
async def list_stores_endpoint():
    """All stores in the registry (credentials redacted)."""
    from agent.multistore import list_stores, public_store

    return {"stores": [public_store(r) for r in await list_stores()]}


@router.get("/stores/summary")
async def stores_summary_endpoint():
    """Fleet health for the Stores page in one call: how many stores are connected
    to each integration, plus each store's integration flags and product-sync
    state. Registered BEFORE /stores/{store_id} so 'summary' isn't captured as an id."""
    from agent.multistore import list_stores, public_store
    from agent.product_sync import get_sync_status

    records = await list_stores()
    stores_out: list[dict[str, Any]] = []
    counts = {"total": 0, "shopify": 0, "gorgias": 0, "shipengine": 0, "subscriptions": 0}
    sync_running = 0

    for rec in records:
        pub = public_store(rec)
        integrations = {
            "shopify": bool(pub.get("has_shopify_token")),
            "gorgias": bool(pub.get("has_gorgias_token")),
            "shipengine": bool(pub.get("has_shipengine_token")),
            "recharge": bool(pub.get("has_recharge_token")),
            "skio": bool(pub.get("has_skio_token")),
        }
        sync = get_sync_status(scope=rec["id"])
        sync_status = sync.get("status") or "idle"
        if sync_status in ("scheduled", "running"):
            sync_running += 1

        counts["total"] += 1
        counts["shopify"] += int(integrations["shopify"])
        counts["gorgias"] += int(integrations["gorgias"])
        counts["shipengine"] += int(integrations["shipengine"])
        counts["subscriptions"] += int(
            integrations["recharge"]
            or integrations["skio"]
            or bool(pub.get("subscription_provider"))
        )

        stores_out.append(
            {
                "id": pub["id"],
                "name": pub["name"],
                "shop_domain": pub.get("shop_domain"),
                "integrations": integrations,
                "subscription_provider": pub.get("subscription_provider"),
                "sync": {
                    "status": sync_status,
                    "last_sync_at": sync.get("finished_at") or sync.get("started_at"),
                    "products_seen": sync.get("products_seen", 0),
                    "error": sync.get("error"),
                },
            }
        )

    return {"counts": counts, "sync_running": sync_running, "stores": stores_out}


@router.post("/stores", status_code=201)
async def create_store_endpoint(req: StoreCreateRequest):
    from agent.multistore import create_store, public_store

    if await _find_duplicate_shop_domain(req.shop_domain):
        raise APIError(
            code="STORE_ALREADY_EXISTS",
            message=f"A store with shop domain {req.shop_domain} already exists.",
            status=409,
        )
    credentials = {}
    if req.shopify_access_token:
        credentials["shopify_access_token"] = req.shopify_access_token
    credentials.update({k: v for k, v in _integration_credentials(req).items() if v is not None})
    rec = await create_store(req.name, shop_domain=req.shop_domain, credentials=credentials)
    return {"store": public_store(rec)}


@router.get("/stores/{store_id}")
async def get_store_endpoint(store_id: str):
    from agent.multistore import get_store, public_store

    rec = await get_store(store_id)
    if rec is None:
        raise_not_found("store", store_id)
    return {"store": public_store(rec)}


@router.patch("/stores/{store_id}")
async def update_store_endpoint(store_id: str, req: StoreUpdateRequest):
    from agent.multistore import public_store, update_store

    if req.shop_domain and await _find_duplicate_shop_domain(req.shop_domain, exclude_id=store_id):
        raise APIError(
            code="STORE_ALREADY_EXISTS",
            message=f"A store with shop domain {req.shop_domain} already exists.",
            status=409,
        )
    credentials = {}
    if req.shopify_access_token is not None:
        # None value on a credential key means "remove"; empty string clears it.
        credentials["shopify_access_token"] = req.shopify_access_token or None
    credentials.update(_integration_credentials(req))
    rec = await update_store(
        store_id,
        name=req.name,
        shop_domain=req.shop_domain,
        credentials=credentials or None,
    )
    if rec is None:
        raise_not_found("store", store_id)
    return {"store": public_store(rec)}


@router.delete("/stores/{store_id}")
async def delete_store_endpoint(store_id: str):
    """Removes the store from the registry and its caches. The per-store data
    file stays on disk — no silent data destruction; re-adding the store
    re-attaches to the same file."""
    from agent.multistore import delete_store

    if not await delete_store(store_id):
        raise_not_found("store", store_id)
    return {"deleted": True, "store_id": store_id}
