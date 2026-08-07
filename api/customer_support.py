"""
Customer Support API Routes.
Endpoints for ticket ingestion (manual + Gorgias webhook), suggestions, responses, analytics.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, validator

from agent.auth import check_shared_secret, verify_api_key
from agent.config import settings
from agent.knowledge_base import knowledge_base
from agent.models import (
    MessageSender,
    SupportAnalytics,
    SupportTicket,
    TicketChannel,
)
from agent.rate_limit import rate_limit_default, rate_limit_refund, rate_limit_resend
from agent.storage import store
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

    @validator("customer_email")
    def validate_email(cls, v):
        if len(v) > 254:
            raise ValueError("customer_email too long (max 254 chars)")
        if "@" not in v:
            raise ValueError("invalid email format")
        return v.strip()

    @validator("body")
    def validate_body(cls, v):
        if len(v) > 4000:
            raise ValueError("message body too long (max 4000 chars)")
        return v.strip()

    @validator("subject")
    def validate_subject(cls, v):
        if len(v) > 200:
            raise ValueError("subject too long (max 200 chars)")
        return v.strip()

    @validator("customer_name")
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
    return {
        "tickets": [r["ticket"] for r in rows],
        "total": len(rows),
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

    @validator("body")
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


class RefundActionRequest(BaseModel):
    amount: float
    reason: str = "Approved by support team"
    notify_customer: bool = True


@router.post("/tickets/{ticket_id}/actions/refund", dependencies=[Depends(rate_limit_refund)])
async def approve_refund(
    ticket_id: str,
    req: RefundActionRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    """Executes a REAL refund on the linked Shopify order. This endpoint is never called
    automatically by the agent — auto-send is hard-blocked whenever a suggested_action is
    present (see support_agent._should_auto_send). A human always calls this explicitly,
    e.g. by clicking 'Approve refund' on the AI's suggested_action in your dashboard.

    Requires an `Idempotency-Key` header (e.g. a UUID your dashboard generates once per
    click). If the same key is sent twice — a retried request, a double-click, a network
    retry — the second call returns the FIRST call's result instead of refunding twice.

    The requested amount is capped at the order's total_price. This does not track
    cumulative prior partial refunds on the same order — for a client doing partial
    refunds regularly, extend this check to subtract already-refunded amounts (Shopify's
    order object includes a `refunds` array you can sum)."""
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
    if req.amount > order_total:
        raise APIError(
            code="REFUND_EXCEEDS_TOTAL",
            message=f"Refund amount {req.amount} exceeds order total {order_total} — refusing to process",
            status=400,
            details={"amount": req.amount, "order_total": order_total},
        )

    try:
        result = await _agent.shopify.create_refund(
            order_id=order_id,
            amount=req.amount,
            reason=req.reason,
            notify_customer=req.notify_customer,
        )
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

    @validator("customer_email")
    def validate_email(cls, v):
        if len(v) > 254:
            raise ValueError("customer_email too long (max 254 chars)")
        if "@" not in v:
            raise ValueError("invalid email format")
        return v.strip()

    @validator("body")
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

    ticket_id = (
        f"inbound_{uuid.uuid4().hex[:12]}" if req.thread_id else f"inbound_{uuid.uuid4().hex[:12]}"
    )
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
        "timestamp": datetime.now(UTC).isoformat(),
    }
