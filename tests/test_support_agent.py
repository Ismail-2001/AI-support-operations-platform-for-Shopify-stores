"""Tests for CustomerSupportAgent orchestration: memory, order-context wiring,
auto-send gating, escalation rules, and the action-blocks-autosend safety rule."""

import pytest

import agent.storage as storage_module
import agent.support_agent as sa
from agent.config import settings
from agent.models import ActionType, SuggestedAction, SupportTicket, TicketCategory, TicketPriority
from agent.support_agent import CustomerSupportAgent
from tests.conftest import FakeClassifier, FakeResponseEngine, FakeShopify

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _restore_store_singletons():
    """_wire_agent() swaps agent.storage.store and agent.support_agent.store for
    its tests. Restore both afterwards - a leaked raw TicketStore breaks
    context-aware routing (X-Store-Id) for every later test in the session."""

    orig_storage = storage_module.store
    orig_sa = sa.store
    yield
    storage_module.store = orig_storage
    sa.store = orig_sa


def _wire_agent(test_store, classifier=None, response_engine=None, shopify=None):
    storage_module.store = test_store
    sa.store = test_store
    agent = CustomerSupportAgent.__new__(CustomerSupportAgent)
    agent.classifier = classifier or FakeClassifier()
    agent.response_engine = response_engine or FakeResponseEngine()
    agent.shopify = shopify or FakeShopify()
    return agent


async def test_new_ticket_seeds_thread_with_one_message(test_store):
    agent = _wire_agent(test_store)
    ticket = SupportTicket(
        id="t1", customer_email="a@b.com", subject="Hi", body="Where is my order?"
    )
    await agent.handle_ticket(ticket)

    thread = await test_store.get_messages("t1")
    assert len(thread) == 1
    assert thread[0].content == "Where is my order?"


async def test_followup_appends_to_same_thread(test_store):
    agent = _wire_agent(test_store)
    ticket = SupportTicket(
        id="t2", customer_email="a@b.com", subject="Hi", body="Where is my order?"
    )
    await agent.handle_ticket(ticket)
    await agent.handle_followup("t2", "Still nothing after 3 days")

    thread = await test_store.get_messages("t2")
    assert len(thread) == 2
    assert thread[1].content == "Still nothing after 3 days"


async def test_followup_on_unknown_ticket_returns_none(test_store):
    agent = _wire_agent(test_store)
    decision = await agent.handle_followup("does-not-exist", "hello")
    assert decision is None


async def test_order_context_fetched_for_order_related_category(test_store):
    class FakeShopifyWithOrder:
        enabled = True

        async def get_order_by_number(self, order_number):
            return {
                "id": 42,
                "name": f"#{order_number}",
                "created_at": "2026-07-01",
                "fulfillment_status": "fulfilled",
                "financial_status": "paid",
                "line_items": [{"quantity": 1, "title": "Hoodie"}],
                "total_price": "49.99",
                "currency": "USD",
                "fulfillments": [],
            }

        def summarize_order(self, order):
            return f"Order {order['name']} — {order['fulfillment_status']}"

    agent = _wire_agent(
        test_store,
        classifier=FakeClassifier(
            category=TicketCategory.ORDER_STATUS, extracted_order_number="1042"
        ),
        shopify=FakeShopifyWithOrder(),
    )
    ticket = SupportTicket(id="t3", customer_email="a@b.com", subject="Order", body="order #1042?")
    decision = await agent.handle_ticket(ticket)
    assert decision.order_context_used is True


async def test_order_context_not_fetched_for_unrelated_category(test_store):
    agent = _wire_agent(
        test_store, classifier=FakeClassifier(category=TicketCategory.PRODUCT_QUESTION)
    )
    ticket = SupportTicket(
        id="t4", customer_email="a@b.com", subject="Q", body="Is this waterproof?"
    )
    decision = await agent.handle_ticket(ticket)
    assert decision.order_context_used is False


async def test_auto_send_requires_flag_enabled(test_store):
    settings.AUTO_SEND_ENABLED = False
    agent = _wire_agent(test_store, response_engine=FakeResponseEngine(confidence=0.99))
    ticket = SupportTicket(id="t5", customer_email="a@b.com", subject="Q", body="hi")
    decision = await agent.handle_ticket(ticket)
    assert decision.auto_sent is False


async def test_auto_send_blocked_below_confidence_threshold(test_store):
    settings.AUTO_SEND_ENABLED = True
    settings.AUTO_SEND_MIN_CONFIDENCE = 0.85
    agent = _wire_agent(test_store, response_engine=FakeResponseEngine(confidence=0.5))
    ticket = SupportTicket(id="t6", customer_email="a@b.com", subject="Q", body="hi")
    decision = await agent.handle_ticket(ticket)
    assert decision.auto_sent is False


async def test_auto_send_blocked_for_refund_category_even_at_high_confidence(test_store):
    settings.AUTO_SEND_ENABLED = True
    agent = _wire_agent(
        test_store,
        classifier=FakeClassifier(category=TicketCategory.REFUND),
        response_engine=FakeResponseEngine(confidence=0.99, requires_human_review=False),
    )
    ticket = SupportTicket(
        id="t7", customer_email="a@b.com", subject="Refund", body="I want a refund"
    )
    decision = await agent.handle_ticket(ticket)
    assert decision.auto_sent is False, (
        "refund category must never auto-send regardless of confidence"
    )


async def test_auto_send_blocked_when_suggested_action_present(test_store):
    settings.AUTO_SEND_ENABLED = True
    action = SuggestedAction(type=ActionType.REFUND, order_id="1", amount=10.0, reason="damaged")
    agent = _wire_agent(
        test_store,
        response_engine=FakeResponseEngine(
            confidence=0.99, requires_human_review=False, suggested_action=action
        ),
    )
    ticket = SupportTicket(id="t8", customer_email="a@b.com", subject="Broken", body="it broke")
    decision = await agent.handle_ticket(ticket)
    assert decision.auto_sent is False, "any suggested_action must block auto-send"


async def test_auto_send_succeeds_when_all_conditions_met(test_store):
    settings.AUTO_SEND_ENABLED = True
    agent = _wire_agent(
        test_store,
        classifier=FakeClassifier(category=TicketCategory.SHIPPING),
        response_engine=FakeResponseEngine(confidence=0.95, requires_human_review=False),
    )
    ticket = SupportTicket(
        id="t9", customer_email="a@b.com", subject="Shipping", body="when will it arrive"
    )
    decision = await agent.handle_ticket(ticket)
    assert decision.auto_sent is True

    # And the AI reply should now be logged into the thread for future context
    thread = await test_store.get_messages("t9")
    assert len(thread) == 2
    assert thread[1].sender_type.value == "ai"


async def test_repeat_contact_forces_escalation_to_urgent(test_store):
    agent = _wire_agent(test_store, classifier=FakeClassifier(priority=TicketPriority.NORMAL))
    ticket = SupportTicket(id="t10", customer_email="a@b.com", subject="Q", body="msg 1")
    await agent.handle_ticket(ticket)
    d2 = await agent.handle_followup("t10", "msg 2")
    assert d2.classification.priority == TicketPriority.NORMAL, (
        "should not escalate before threshold"
    )

    d3 = await agent.handle_followup("t10", "msg 3, still no answer!!")
    assert d3.classification.priority == TicketPriority.URGENT, "3rd contact must force-escalate"
    assert d3.suggestion.requires_human_review is True


async def test_repeat_contact_does_not_downgrade_already_critical_priority(test_store):
    agent = _wire_agent(test_store, classifier=FakeClassifier(priority=TicketPriority.CRITICAL))
    ticket = SupportTicket(id="t11", customer_email="a@b.com", subject="Q", body="msg 1")
    await agent.handle_ticket(ticket)
    await agent.handle_followup("t11", "msg 2")
    d3 = await agent.handle_followup("t11", "msg 3")
    assert d3.classification.priority == TicketPriority.CRITICAL, (
        "escalation must never LOWER priority"
    )


# ── H1: requester ↔ order identity binding ──────────────────────


async def test_matching_email_grants_order_view():
    from agent.graph import requester_may_view_order
    from agent.models import TicketChannel

    ticket = SupportTicket(id="v1", customer_email="Buyer@Example.COM", subject="s", body="b")
    assert requester_may_view_order(ticket, {"email": "buyer@example.com"}) is True
    assert requester_may_view_order(ticket, {"customer": {"email": "buyer@example.com"}}) is True
    chat_ticket = SupportTicket(
        id="v2",
        customer_email="buyer@example.com",
        subject="s",
        body="b",
        channel=TicketChannel.CHAT,
    )
    assert requester_may_view_order(chat_ticket, {"email": "buyer@example.com"}) is True


async def test_mismatched_email_denies_order_view():
    from agent.graph import requester_may_view_order

    ticket = SupportTicket(id="v3", customer_email="attacker@evil.test", subject="s", body="b")
    assert requester_may_view_order(ticket, {"email": "victim@shop.test"}) is False
    assert requester_may_view_order(ticket, {"customer": {"email": "victim@shop.test"}}) is False


async def test_anonymous_chat_is_always_denied():
    from agent.graph import requester_may_view_order
    from agent.models import TicketChannel

    guest = SupportTicket(
        id="v4",
        customer_email="guest+abcd1234@chat-widget.local",
        subject="s",
        body="b",
        channel=TicketChannel.CHAT,
    )
    # Order has an identity and the guest has one - they cannot match.
    assert requester_may_view_order(guest, {"email": "victim@shop.test"}) is False
    # No identity on the order either - anonymous chat still gets nothing.
    assert requester_may_view_order(guest, {}) is False


async def test_internal_channel_without_comparable_identity_allows():
    """Agent/operator tickets on orders with no email on file fall back to the
    channel's own trust (X-API-Key / verified inbound), keeping internal flows working."""
    from agent.graph import requester_may_view_order

    ticket = SupportTicket(id="v5", customer_email="ops@agency.test", subject="s", body="b")
    assert requester_may_view_order(ticket, {"name": "#1001"}) is True


async def test_graph_denies_order_context_for_mismatched_chat_requester(test_store):
    class FakeShopifyOrderWithEmail:
        enabled = True

        async def get_order_by_number(self, order_number):
            return {
                "id": 42,
                "name": f"#{order_number}",
                "email": "real-owner@shop.test",
                "fulfillment_status": "unfulfilled",
                "financial_status": "paid",
                "line_items": [{"quantity": 1, "title": "Hoodie"}],
                "total_price": "49.99",
                "fulfillments": [],
            }

        def summarize_order(self, order):
            return f"SECRET-CONTEXT {order['name']}"

    from agent.models import TicketChannel

    agent = _wire_agent(
        test_store,
        classifier=FakeClassifier(
            category=TicketCategory.ORDER_STATUS, extracted_order_number="1042"
        ),
        shopify=FakeShopifyOrderWithEmail(),
    )
    ticket = SupportTicket(
        id="t-h1",
        customer_email="guest+deadbeef@chat-widget.local",
        subject="Order",
        body="where is order #1042?",
        channel=TicketChannel.CHAT,
    )
    decision = await agent.handle_ticket(ticket)
    assert decision.order_context_used is False
