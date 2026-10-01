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
    def __init__(self):
        self.classifier = TicketClassifier()
        self.response_engine = ResponseGenerationEngine()
        self.shopify = ShopifyClient()
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
            auto_sent=final_state["auto_sent"],
        )
