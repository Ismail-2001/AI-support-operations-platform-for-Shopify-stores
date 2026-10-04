"""Public storefront chat endpoints — the embeddable widget's backend.

Auth model: the **publishable widget key** (`X-Widget-Key` header or `?key=` query),
never the private API key. The widget ships inside a public <script> tag, so the key
can only do what a stranger on the internet should be allowed to do: open a chat
session and send messages, rate-limited per IP. Money-moving and data-reading
capabilities all stay behind `X-API-Key` on /support/*.

Pipeline: identical to the Gorgias path — every chat message becomes a real ticket
(channel=chat) run through the same LangGraph graph (classify → order lookup → KB →
draft → confidence gate → persist). Replies stream back as SSE with a per-node stage
event so the widget shows live progress instead of a blank spinner, then one final
`message` event carrying confidence + whether a human should take over.

Design note on "streaming": the pipeline's two LLM calls are structured-output
calls (JSON with confidence/actions), which no provider streams cleanly. Rather
than double token cost with a parallel raw-stream call, we stream *pipeline stages*
as they happen and reveal the final text client-side — same cost, same guarantees,
immediately responsive UX.
"""

import hmac
import json
import uuid
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent.automation import get_min_confidence
from agent.config import settings
from agent.models import AgentDecision, SupportTicket, TicketChannel
from agent.rate_limit import rate_limit_chat
from agent.storage import store
from agent.widget_store import get_widget_config, get_widget_key
from api.errors import APIError, raise_not_found, raise_validation_error

logger = structlog.get_logger(__name__)

chat_router = APIRouter(prefix="/chat", tags=["chat"], dependencies=[Depends(rate_limit_chat)])

_WIDGET_JS_PATH = Path(__file__).parent / "static" / "cs-chat.js"

_MAX_MESSAGE_CHARS = 4000

# Friendly labels for graph nodes — sent alongside raw stage names so the widget
# can render human text immediately (and tests can assert on stable names).
STAGE_LABELS = {
    "load_history": "Loading conversation…",
    "classify_ticket": "Understanding your message…",
    "apply_escalation": "Checking conversation history…",
    "fetch_order_context": "Looking up your order…",
    "fetch_knowledge_context": "Checking our help docs…",
    "fetch_subscription_context": "Checking your subscription…",
    "generate_response": "Writing a reply…",
    "decide_auto_send": "Reviewing confidence…",
    "save_results": "Saving…",
}


# ── Auth ──────────────────────────────────────────────────────────────────────


async def _expected_widget_key() -> str:
    return await get_widget_key()


def _provided_widget_key(request: Request, header_key: str | None) -> str:
    return header_key or request.query_params.get("key") or ""


async def verify_widget_key(
    request: Request, x_widget_key: str | None = Header(default=None)
) -> None:
    provided = _provided_widget_key(request, x_widget_key)
    expected = await _expected_widget_key()
    if not provided or not hmac.compare_digest(provided, expected):
        raise APIError(code="INVALID_WIDGET_KEY", message="Invalid widget key", status=401)


async def _require_enabled() -> dict[str, Any]:
    config = await get_widget_config()
    if not config["enabled"]:
        raise APIError(code="WIDGET_DISABLED", message="Chat widget is turned off", status=403)
    return config


# ── Request models ────────────────────────────────────────────────────────────


class SessionCreateRequest(BaseModel):
    email: str | None = Field(default=None, max_length=200)
    name: str | None = Field(default=None, max_length=120)
    order_number: str | None = Field(default=None, max_length=60)
    session_id: str | None = Field(default=None, max_length=64)


class MessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=_MAX_MESSAGE_CHARS)


class HandoffRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _load_history(ticket_id: str | None) -> list[dict[str, Any]]:
    if not ticket_id:
        return []
    messages = await store.get_messages(ticket_id)
    role_for = {"customer": "customer", "ai": "assistant", "agent": "agent"}
    return [
        {
            "role": role_for.get(m.sender_type.value, "assistant"),
            "content": m.content,
            "at": m.created_at,
        }
        for m in messages
    ]


def _classify_session_create(req: SessionCreateRequest) -> None:
    if req.email and ("@" not in req.email or req.email.strip() != req.email):
        raise_validation_error("email", "must be a valid email address")


def _new_ticket(session_id: str, session_data: dict[str, Any], body: str) -> SupportTicket:
    email = session_data.get("email") or f"guest+{session_id[:8]}@chat-widget.local"
    return SupportTicket(
        id=f"chat_{uuid.uuid4().hex[:12]}",
        customer_email=email,
        customer_name=session_data.get("name"),
        subject="Storefront chat",
        body=body,
        channel=TicketChannel.CHAT,
        order_number=session_data.get("order_number") or None,
        metadata={"widget_session": session_id},
    )


async def _needs_human(decision: AgentDecision) -> bool:
    """Mirror of the decide_auto_send gate, but expressed for the widget: should a
    person follow up on this conversation? Same thresholds as auto-send, so the
    widget never claims more confidence than the send policy would allow."""
    suggestion = decision.suggestion
    if suggestion.requires_human_review:
        return True
    if suggestion.suggested_action and suggestion.suggested_action.type.value != "none":
        return True
    category = decision.classification.category.value
    blocked = {c.strip() for c in settings.AUTO_SEND_BLOCKED_CATEGORIES.split(",") if c.strip()}
    if category in blocked:
        return True
    min_confidence = await get_min_confidence(category)
    return suggestion.confidence < min_confidence


def _message_payload(decision: AgentDecision, needs_human: bool) -> dict[str, Any]:
    suggestion = decision.suggestion
    action = (
        suggestion.suggested_action.model_dump(mode="json") if suggestion.suggested_action else None
    )
    return {
        "role": "assistant",
        "ticket_id": decision.ticket_id,
        "content": suggestion.suggested_response,
        "confidence": suggestion.confidence,
        "category": decision.classification.category.value,
        "priority": decision.classification.priority.value,
        "requires_human_review": suggestion.requires_human_review,
        "needs_human": needs_human,
        "action": action,
        "kb_used": decision.kb_used,
        "order_context_used": decision.order_context_used,
        "auto_sent": decision.auto_sent,
    }


async def _stream_events(session_id: str, stream, config: dict[str, Any]):
    """Consumes support_agent's event stream and frames it as SSE. Any mid-stream
    failure degrades to an error event — the widget shows a retry, never a hung
    spinner (the backend being down must not break the storefront page)."""
    ticket_id: str | None = None
    try:
        async for event in stream:
            etype = event.get("type")
            if etype == "stage":
                node = event["stage"]
                yield _sse(
                    "stage",
                    {"stage": node, "label": STAGE_LABELS.get(node, "Working…")},
                )
            elif etype == "error":
                yield _sse("error", {"error": event.get("code"), "message": event.get("message")})
            elif etype == "message":
                decision: AgentDecision = event["decision"]
                ticket_id = decision.ticket_id
                needs_human = await _needs_human(decision)
                payload = _message_payload(decision, needs_human)
                payload["show_confidence"] = bool(config.get("show_confidence"))
                yield _sse("message", payload)
        yield _sse("done", {"ticket_id": ticket_id})
    except Exception as exc:
        logger.error("chat_stream_failed", session_id=session_id, error=str(exc))
        yield _sse(
            "error",
            {
                "error": "STREAM_FAILED",
                "message": "Something went wrong on our side. Please try again.",
            },
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@chat_router.get("/widget.js")
async def widget_script():
    """The embeddable widget itself — one script tag, no build step for the store."""
    if not _WIDGET_JS_PATH.exists():
        raise APIError(
            code="WIDGET_NOT_BUILT",
            message="Widget bundle missing — run `npm run build:widget` in dashboard/.",
            status=404,
        )
    return FileResponse(
        _WIDGET_JS_PATH,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300"},
    )


@chat_router.get("/config", dependencies=[Depends(verify_widget_key)])
async def widget_public_config():
    """What the widget needs to render itself: appearance + greeting."""
    return await get_widget_config()


@chat_router.post("/sessions", dependencies=[Depends(verify_widget_key)])
async def create_or_resume_session(req: SessionCreateRequest):
    """Create a chat session, or resume a previous one (conversation continues
    after reload/return — the session id lives in the customer's localStorage)."""
    _classify_session_create(req)

    if req.session_id:
        existing = await store.get_chat_session(req.session_id)
        if existing:
            return {
                "session_id": existing["id"],
                "ticket_id": existing["ticket_id"],
                "history": await _load_history(existing["ticket_id"]),
            }

    session_id = f"cs_{uuid.uuid4().hex[:16]}"
    await store.create_chat_session(
        session_id,
        {
            "email": req.email.strip() if req.email else None,
            "name": req.name.strip() if req.name else None,
            "order_number": req.order_number.strip() if req.order_number else None,
        },
    )
    return {"session_id": session_id, "ticket_id": None, "history": []}


@chat_router.get("/sessions/{session_id}", dependencies=[Depends(verify_widget_key)])
async def get_session(session_id: str):
    session = await store.get_chat_session(session_id)
    if not session:
        raise_not_found("chat_session", session_id)
    return {
        "session_id": session["id"],
        "ticket_id": session["ticket_id"],
        "history": await _load_history(session["ticket_id"]),
    }


@chat_router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str, req: MessageRequest, request: Request, x_widget_key: str | None = Header(None)
):
    """Send a customer message; replies stream back as SSE events
    (stage → stage → … → message → done | error)."""
    await verify_widget_key(request, x_widget_key)
    config = await _require_enabled()

    session = await store.get_chat_session(session_id)
    if not session:
        raise_not_found("chat_session", session_id)

    body = req.body.strip()
    if not body:
        raise_validation_error("body", "message is empty")

    import api.customer_support as cs  # late import: tests reload this module's _agent

    ticket_id = session["ticket_id"]
    if ticket_id and not await store.get(ticket_id):
        # Session points at a ticket that never got persisted (pipeline failed earlier).
        ticket_id = None

    if ticket_id:
        stream = cs._agent.handle_followup_stream(ticket_id, body)
    else:
        ticket = _new_ticket(session_id, session["data"], body)
        ticket_id = ticket.id
        await store.update_chat_session(session_id, ticket_id=ticket_id)
        stream = cs._agent.handle_ticket_stream(ticket)

    return StreamingResponse(
        _stream_events(session_id, stream, config),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@chat_router.post("/sessions/{session_id}/handoff", dependencies=[Depends(verify_widget_key)])
async def request_handoff(session_id: str, req: HandoffRequest):
    """Customer tapped "Talk to a human". Flags the underlying ticket so it shows up
    in the operator console with full context — no separate inbox, no lost thread."""
    await _require_enabled()
    session = await store.get_chat_session(session_id)
    if not session:
        raise_not_found("chat_session", session_id)
    if not session["ticket_id"]:
        raise APIError(
            code="NO_TICKET_YET",
            message="Start the conversation before requesting a handoff",
            status=409,
        )

    row = await store.get(session["ticket_id"])
    if not row:
        raise_not_found("ticket", session["ticket_id"])

    ticket = row["ticket"]
    metadata = dict(ticket.get("metadata") or {})
    metadata["widget_handoff"] = True
    if req.reason:
        metadata["widget_handoff_reason"] = req.reason.strip()[:500]
    from datetime import UTC, datetime

    metadata["widget_handoff_at"] = datetime.now(UTC).isoformat()
    await store.update_chat_session(session_id, merge={"handoff": True})
    await store.update_status(session["ticket_id"], metadata=metadata)
    logger.info(
        "chat_handoff_requested",
        session_id=session_id,
        ticket_id=session["ticket_id"],
        reason=bool(req.reason),
    )
    return {"ticket_id": session["ticket_id"], "status": "handoff_requested"}
