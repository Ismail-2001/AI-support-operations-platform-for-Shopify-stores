"""Conversation windowing tests (WP5).

The LLM prompt gets only the newest MAX_HISTORY_MESSAGES messages (flat token
cost on long tickets), while escalation decisions keep counting the FULL
thread — a small window must never hide repeat contact from the graph."""

from tests.conftest import FakeClassifier, FakeResponseEngine, FakeShopify


async def _add_thread(store, ticket_id: str, n_customer: int, n_agent: int = 0):
    for i in range(n_customer):
        await store.add_message(ticket_id, "customer", f"customer {i}")
    for i in range(n_agent):
        await store.add_message(ticket_id, "agent", f"agent {i}")


async def test_get_messages_limit_returns_newest_window_chronologically(test_store):
    from agent.models import SupportTicket

    await test_store.save(SupportTicket(id="t1", customer_email="a@b.com", subject="s", body="b"))
    for i in range(10):
        await test_store.add_message("t1", "customer", f"msg {i}")

    windowed = await test_store.get_messages("t1", limit=4)
    # Newest 4, still oldest-first inside the window.
    assert [m.content for m in windowed] == ["msg 6", "msg 7", "msg 8", "msg 9"]

    # Default (no limit) stays the full transcript — operator views depend on it.
    assert len(await test_store.get_messages("t1")) == 10

    # limit >= total: everything. limit 0: treated as unlimited.
    assert len(await test_store.get_messages("t1", limit=100)) == 10
    assert len(await test_store.get_messages("t1", limit=0)) == 10


async def test_count_customer_messages_covers_full_thread(test_store):
    from agent.models import SupportTicket

    await test_store.save(SupportTicket(id="t2", customer_email="a@b.com", subject="s", body="b"))
    await _add_thread(test_store, "t2", n_customer=5, n_agent=2)

    assert await test_store.count_customer_messages("t2") == 5
    assert await test_store.count_customer_messages("no-such-ticket") == 0


async def test_prompt_windowed_but_escalation_counts_full_thread(test_store, monkeypatch):
    """With MAX_HISTORY_MESSAGES below REPEAT_CONTACT_ESCALATION_THRESHOLD, a
    repeat-contact ticket must STILL escalate (the count is not windowed) while
    the classifier only sees the window."""
    import agent.storage as storage_module
    from agent.config import settings
    from agent.models import Sentiment, SupportTicket, TicketCategory, TicketPriority
    from agent.support_agent import CustomerSupportAgent

    monkeypatch.setattr(settings, "MAX_HISTORY_MESSAGES", 2)

    # Same wiring trick as test_automation: bare agent + swapped store singletons.
    storage_module.store = test_store
    import agent.support_agent as sa

    sa.store = test_store

    agent = CustomerSupportAgent.__new__(CustomerSupportAgent)

    captured: dict = {}

    class RecordingClassifier(FakeClassifier):
        async def classify(self, ticket, history=None):
            captured["prompt_messages"] = len(history or [])
            return await super().classify(ticket, history)

    agent.classifier = RecordingClassifier(
        category=TicketCategory.ORDER_STATUS,
        priority=TicketPriority.NORMAL,
        sentiment=Sentiment.NEUTRAL,
    )
    agent.response_engine = FakeResponseEngine(confidence=0.9, requires_human_review=False)
    agent.shopify = FakeShopify()

    ticket = SupportTicket(id="esc1", customer_email="a@b.com", subject="Q", body="again!")
    await test_store.save(ticket)
    # 5 customer messages: window=2 shows only the last 2 to the model.
    await _add_thread(test_store, "esc1", n_customer=5)

    decision = await agent.handle_ticket(ticket)
    assert decision is not None

    # The prompt saw only the window...
    assert captured["prompt_messages"] == 2

    # ...but escalation saw all 5 customer messages (threshold is 3) and
    # raised priority to urgent.
    row = await test_store.get("esc1")
    assert row["ticket"]["priority"] == TicketPriority.URGENT.value
