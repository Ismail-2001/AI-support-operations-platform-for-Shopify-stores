"""LangGraph StateGraph for the customer support pipeline.

This replaces the fixed procedural pipeline in support_agent.py's _process()
with an explicitly traced graph. Every node is still deterministic Python code —
the safety gates (auto-send policy, escalation rules, category-based data fetching)
are all hard-coded in nodes, NOT left to the LLM to decide.

The public interface of CustomerSupportAgent (handle_ticket, handle_followup) is
unchanged — only _process() is swapped to invoke the graph.
"""

import time
from typing import Any, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph

from agent.automation import get_min_confidence
from agent.config import settings
from agent.models import (
    ClassificationResult,
    MessageSender,
    ResponseSuggestion,
    SupportTicket,
    TicketMessage,
    TicketPriority,
)
from agent.observability import record_graph_step

logger = structlog.get_logger(__name__)

ORDER_RELEVANT_CATEGORIES = {"order_status", "shipping", "returns", "refund"}
KB_RELEVANT_CATEGORIES = {
    "product_question",
    "returns",
    "refund",
    "shipping",
    "technical",
    "other",
    "subscription",
}
SUBSCRIPTION_RELEVANT_CATEGORIES = {"subscription"}
REPEAT_CONTACT_ESCALATION_THRESHOLD = 3
_PRIORITY_ORDER = [
    TicketPriority.LOW,
    TicketPriority.NORMAL,
    TicketPriority.HIGH,
    TicketPriority.URGENT,
    TicketPriority.CRITICAL,
]


class AgentState(TypedDict):
    ticket: SupportTicket
    history: list[TicketMessage]
    customer_message_count: int
    classification: ClassificationResult | None
    order_context: str | None
    order_used: bool
    knowledge_context: str | None
    kb_used: bool
    subscription_context: str | None
    subscription_used: bool
    suggestion: ResponseSuggestion | None
    auto_sent: bool
    dry_run: bool


def build_agent_graph(classifier, response_engine, shopify):
    """Build and return a compiled StateGraph.

    The graph nodes capture ``classifier`` / ``response_engine`` / ``shopify``
    by closure, so tests can wire any mocks they like before ``_process()``
    first accesses the graph.
    """
    workflow = StateGraph(AgentState)

    # ── Node implementations ─────────────────────────────────────

    async def load_history(state: AgentState) -> dict[str, Any]:
        from agent.storage import store

        history = await store.get_messages(state["ticket"].id)
        customer_message_count = sum(1 for m in history if m.sender_type.value == "customer")
        return {
            "history": history,
            "customer_message_count": customer_message_count,
        }

    async def classify_ticket(state: AgentState) -> dict[str, Any]:
        result = await classifier.classify(state["ticket"], history=state["history"])
        return {"classification": result}

    async def apply_escalation(state: AgentState) -> dict[str, Any]:
        classification = state["classification"]
        count = state["customer_message_count"]
        if count >= REPEAT_CONTACT_ESCALATION_THRESHOLD:
            current_idx = _PRIORITY_ORDER.index(classification.priority)
            urgent_idx = _PRIORITY_ORDER.index(TicketPriority.URGENT)
            if current_idx < urgent_idx:
                logger.info(
                    "priority_escalated_repeat_contact",
                    old_priority=classification.priority.value,
                    new_priority=TicketPriority.URGENT.value,
                    customer_message_count=count,
                )
                classification = classification.model_copy(
                    update={"priority": TicketPriority.URGENT}
                )
        return {"classification": classification}

    async def fetch_order_context(state: AgentState) -> dict[str, Any]:
        ticket = state["ticket"]
        classification = state["classification"]
        if classification.category.value not in ORDER_RELEVANT_CATEGORIES:
            return {"order_context": None, "order_used": False}

        order_number = ticket.order_number or classification.extracted_order_number
        if not order_number:
            return {"order_context": None, "order_used": False}

        if not shopify.enabled:
            return {
                "order_context": "Shopify is not connected — cannot look up order data.",
                "order_used": False,
            }

        try:
            order = await shopify.get_order_by_number(order_number)
        except Exception as e:
            logger.warning("shopify_lookup_failed", ticket_id=ticket.id, error=str(e))
            return {
                "order_context": f"Order lookup failed (order #{order_number} not found or API error).",
                "order_used": False,
            }

        if not order:
            return {
                "order_context": f"No order found matching '{order_number}'.",
                "order_used": False,
            }

        ticket.order_id = str(order.get("id"))
        context = shopify.summarize_order(order)
        return {"order_context": context, "order_used": True, "ticket": ticket}

    async def fetch_knowledge_context(state: AgentState) -> dict[str, Any]:
        from agent.knowledge_base import knowledge_base

        classification = state["classification"]
        history = state["history"]
        ticket = state["ticket"]

        if classification.category.value not in KB_RELEVANT_CATEGORIES:
            return {"knowledge_context": None, "kb_used": False}

        query = history[-1].content if history else ticket.body
        try:
            chunks = await knowledge_base.search(query, top_k=3)
        except Exception as e:
            logger.warning("kb_search_failed", error=str(e), ticket_id=ticket.id)
            return {"knowledge_context": None, "kb_used": False}

        if not chunks:
            return {"knowledge_context": None, "kb_used": False}

        block = "\n\n---\n\n".join(f"[{c.source}] {c.title}\n{c.content}" for c in chunks)
        return {"knowledge_context": block, "kb_used": True}

    async def fetch_subscription_context(state: AgentState) -> dict[str, Any]:
        from integrations.subscriptions import (
            SubscriptionNotConfigured,
            SubscriptionService,
        )

        classification = state["classification"]
        ticket = state["ticket"]
        if classification.category.value not in SUBSCRIPTION_RELEVANT_CATEGORIES:
            return {"subscription_context": None, "subscription_used": False}
        if not ticket.customer_email:
            return {"subscription_context": None, "subscription_used": False}

        service = SubscriptionService()
        if not service.enabled:
            # Honest "we can't see it yet" — the response prompt must never invent
            # subscription state, and never claim an action was performed.
            return {
                "subscription_context": "Subscription app is not connected — the agent "
                "cannot see or change subscriptions.",
                "subscription_used": False,
            }

        try:
            subs = await service.list_subscriptions(ticket.customer_email)
        except SubscriptionNotConfigured as e:
            return {
                "subscription_context": f"Subscription lookup unavailable: {e}",
                "subscription_used": False,
            }
        except Exception as e:
            logger.warning("subscription_lookup_failed", ticket_id=ticket.id, error=str(e))
            return {
                "subscription_context": "Subscription lookup failed — do not guess subscription "
                "state; say the agent is checking.",
                "subscription_used": False,
            }

        if not subs:
            return {
                "subscription_context": f"No subscriptions found for {ticket.customer_email}.",
                "subscription_used": True,
            }

        lines = []
        for sub in subs:
            addr = sub.address or {}
            addr_bits = ", ".join(
                p
                for p in [
                    addr.get("address1"),
                    addr.get("city"),
                    addr.get("state"),
                    addr.get("zip"),
                ]
                if p
            )
            freq = (
                f"every {sub.frequency_count} {sub.frequency_unit}"
                if sub.frequency_unit
                else "frequency unknown"
            )
            lines.append(
                f"- id={sub.id} provider={sub.provider} status={sub.status} "
                f"title={sub.title} qty={sub.quantity} price={sub.price} "
                f"next_charge={sub.next_charge_date} frequency={freq}"
                + (f" ship_to={addr_bits}" if addr_bits else "")
            )
        block = "Subscriptions for this customer:\n" + "\n".join(lines)
        return {"subscription_context": block, "subscription_used": True}

    async def generate_response(state: AgentState) -> dict[str, Any]:
        suggestion = await response_engine.generate_suggestion(
            state["ticket"],
            state["classification"],
            order_context=state["order_context"],
            knowledge_context=state["knowledge_context"],
            subscription_context=state.get("subscription_context"),
            history=state["history"],
        )
        if state["customer_message_count"] >= REPEAT_CONTACT_ESCALATION_THRESHOLD:
            suggestion.requires_human_review = True
        return {"suggestion": suggestion}

    async def decide_auto_send(state: AgentState) -> dict[str, Any]:
        suggestion = state["suggestion"]
        classification = state["classification"]

        if not settings.AUTO_SEND_ENABLED:
            return {"auto_sent": False}
        if suggestion.requires_human_review:
            return {"auto_sent": False}
        # Per-category confidence floor: .env default, runtime override via
        # PUT /support/automation/thresholds (agent.automation.get_min_confidence).
        min_confidence = await get_min_confidence(classification.category.value)
        if suggestion.confidence < min_confidence:
            return {"auto_sent": False}
        if suggestion.suggested_action and suggestion.suggested_action.type.value != "none":
            return {"auto_sent": False}
        blocked = {c.strip() for c in settings.AUTO_SEND_BLOCKED_CATEGORIES.split(",") if c.strip()}
        if classification.category.value in blocked:
            return {"auto_sent": False}

        from agent.observability import check_daily_cost_cap

        if not await check_daily_cost_cap():
            return {"auto_sent": False}

        return {"auto_sent": True}

    async def save_results(state: AgentState) -> dict[str, Any]:
        from agent.storage import store

        ticket = state["ticket"]
        classification = state["classification"]
        suggestion = state["suggestion"]
        auto_sent = state["auto_sent"]

        ticket.category = classification.category
        ticket.priority = classification.priority
        ticket.sentiment = classification.sentiment

        # Setup-wizard previews run the exact same pipeline but must not leave
        # a fake ticket behind in the store.
        if state.get("dry_run"):
            logger.info(
                "ticket_dry_run",
                ticket_id=ticket.id,
                category=classification.category.value,
                confidence=suggestion.confidence,
            )
            return {"ticket": ticket}

        await store.save(ticket, suggestion, auto_sent=auto_sent)

        if auto_sent:
            await store.add_message(
                ticket.id, MessageSender.AI.value, suggestion.suggested_response
            )

        logger.info(
            "ticket_handled",
            ticket_id=ticket.id,
            category=classification.category.value,
            priority=classification.priority.value,
            confidence=suggestion.confidence,
            order_context_used=state["order_used"],
            kb_used=state["kb_used"],
            auto_sent=auto_sent,
            customer_message_count=state["customer_message_count"],
        )

        return {"ticket": ticket}

    # ── Wire nodes ────────────────────────────────────────

    for name, fn in [
        ("load_history", load_history),
        ("classify_ticket", classify_ticket),
        ("apply_escalation", apply_escalation),
        ("fetch_order_context", fetch_order_context),
        ("fetch_knowledge_context", fetch_knowledge_context),
        ("fetch_subscription_context", fetch_subscription_context),
        ("generate_response", generate_response),
        ("decide_auto_send", decide_auto_send),
        ("save_results", save_results),
    ]:
        workflow.add_node(name, _traced_node(name, fn))

    # ── Linear pipeline edges ──────────────────────────────

    workflow.add_edge(START, "load_history")
    workflow.add_edge("load_history", "classify_ticket")
    workflow.add_edge("classify_ticket", "apply_escalation")
    workflow.add_edge("apply_escalation", "fetch_order_context")
    workflow.add_edge("fetch_order_context", "fetch_knowledge_context")
    workflow.add_edge("fetch_knowledge_context", "fetch_subscription_context")
    workflow.add_edge("fetch_subscription_context", "generate_response")
    workflow.add_edge("generate_response", "decide_auto_send")
    workflow.add_edge("decide_auto_send", "save_results")
    workflow.add_edge("save_results", END)

    return workflow.compile()


def _traced_node(name: str, fn):
    """Wrap a graph node with timing + trace logging to /tickets/{id}/trace."""

    async def wrapper(state: AgentState) -> dict[str, Any]:
        start = time.monotonic()
        try:
            return await fn(state)
        finally:
            latency_ms = (time.monotonic() - start) * 1000
            await record_graph_step(
                ticket_id=state["ticket"].id,
                node_name=name,
                latency_ms=latency_ms,
            )

    return wrapper
