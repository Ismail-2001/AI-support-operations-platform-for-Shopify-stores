"""The orchestrator: classify -> pull real order data + knowledge base context -> draft reply
-> escalation check -> decide what to do with it.

Every ticket is backed by a persisted message thread (agent/storage.py `messages` table).
New ticket -> first customer message. Follow-up -> appended to the same thread, and every
classification/response call sees the WHOLE thread, not just the newest message.

The core pipeline lives in agent/graph.py as a LangGraph StateGraph. This module keeps
the public interface (handle_ticket, handle_followup) unchanged so api/customer_support.py
needs zero changes.
"""

import structlog

from agent.classifier import TicketClassifier
from agent.graph import build_agent_graph
from agent.models import (
    AgentDecision,
    MessageSender,
    SupportTicket,
)
from agent.response_engine import ResponseGenerationEngine
from agent.storage import store
from integrations.shopify import ShopifyClient

logger = structlog.get_logger(__name__)


class CustomerSupportAgent:
    def __init__(self, shopify: ShopifyClient | None = None):
        self.classifier = TicketClassifier()
        self.response_engine = ResponseGenerationEngine()
        # Per-store agents pass a ShopifyClient bound to their own shop domain
        # and token; None keeps the deployment-wide default from settings.
        self.shopify = shopify or ShopifyClient()
        self._graph = None

    @property
    def graph(self):
        if getattr(self, "_graph", None) is None:
            self._graph = build_agent_graph(self.classifier, self.response_engine, self.shopify)
        return self._graph

    async def handle_ticket(self, ticket: SupportTicket) -> AgentDecision:
        """Entry point for a brand-new ticket. Seeds the thread with the customer's first message."""
        await store.add_message(ticket.id, MessageSender.CUSTOMER.value, ticket.body)
        return await self._process(ticket)

    async def dry_run(self, ticket: SupportTicket) -> AgentDecision:
        """Run the full pipeline WITHOUT persisting a ticket — used by the setup
        wizard's test step so a trial question never becomes a real ticket."""
        return await self._process(ticket, dry_run=True)

    async def handle_followup(self, ticket_id: str, message_body: str) -> AgentDecision | None:
        """Entry point for a new customer message on an EXISTING ticket (thread continues)."""
        ticket = await store.get_ticket_model(ticket_id)
        if not ticket:
            return None
        await store.add_message(ticket_id, MessageSender.CUSTOMER.value, message_body)
        return await self._process(ticket)

    async def handle_ticket_stream(self, ticket: SupportTicket):
        """Same as handle_ticket, but yields pipeline progress events instead of
        blocking until the end — the chat widget shows live stage updates over SSE.

        Yields dicts: {"type": "stage", "stage": <graph node>} as each node runs,
        then exactly one {"type": "message", "decision": AgentDecision}. Persistence,
        confidence gating, cost caps — identical to the blocking path (same graph)."""
        await store.add_message(ticket.id, MessageSender.CUSTOMER.value, ticket.body)
        async for event in self._stream(ticket):
            yield event

    async def handle_followup_stream(self, ticket_id: str, message_body: str):
        """Streaming twin of handle_followup. Returns None-ish (yields an error event)
        if the ticket vanished between session lookup and processing."""
        ticket = await store.get_ticket_model(ticket_id)
        if not ticket:
            yield {"type": "error", "code": "TICKET_NOT_FOUND", "message": "Ticket not found"}
            return
        await store.add_message(ticket_id, MessageSender.CUSTOMER.value, message_body)
        async for event in self._stream(ticket):
            yield event

    async def _stream(self, ticket: SupportTicket, dry_run: bool = False):
        """Runs the graph with stream_mode="updates", surfacing each node as a stage
        event and reconstructing the final AgentDecision from the accumulated state."""
        accumulated: dict = {}
        initial = {
            "ticket": ticket,
            "history": [],
            "customer_message_count": 0,
            "classification": None,
            "order_context": None,
            "order_used": False,
            "knowledge_context": None,
            "kb_used": False,
            "subscription_context": None,
            "subscription_used": False,
            "suggestion": None,
            "auto_sent": False,
            "dry_run": dry_run,
        }
        async for update in self.graph.astream(initial, stream_mode="updates"):
            if not isinstance(update, dict):
                continue
            for node_name, node_output in update.items():
                if isinstance(node_output, dict):
                    accumulated.update(node_output)
                yield {"type": "stage", "stage": node_name}

        yield {
            "type": "message",
            "decision": AgentDecision(
                ticket_id=ticket.id,
                classification=accumulated["classification"],
                suggestion=accumulated["suggestion"],
                order_context_used=accumulated.get("order_used", False),
                kb_used=accumulated.get("kb_used", False),
                subscription_used=accumulated.get("subscription_used", False),
                auto_sent=accumulated.get("auto_sent", False),
            ),
        }

    async def _process(self, ticket: SupportTicket, dry_run: bool = False) -> AgentDecision:
        final_state = await self.graph.ainvoke(
            {
                "ticket": ticket,
                "history": [],
                "customer_message_count": 0,
                "classification": None,
                "order_context": None,
                "order_used": False,
                "knowledge_context": None,
                "kb_used": False,
                "subscription_context": None,
                "subscription_used": False,
                "suggestion": None,
                "auto_sent": False,
                "dry_run": dry_run,
            }
        )

        return AgentDecision(
            ticket_id=ticket.id,
            classification=final_state["classification"],
            suggestion=final_state["suggestion"],
            order_context_used=final_state["order_used"],
            kb_used=final_state["kb_used"],
            subscription_used=final_state.get("subscription_used", False),
            auto_sent=final_state["auto_sent"],
        )
