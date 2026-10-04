"""Storefront chat widget API tests: publishable-key auth, session lifecycle,
SSE stage/message framing, needs_human gating, handoff, and failure degradation.

The agent's graph is replaced by fakes that yield the same event dicts the real
stream produces — these tests cover OUR endpoint/SSE/auth logic, not the LLM."""

import json
import os
import sys

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent.rate_limit as rate_limit_module
from agent.config import settings
from agent.models import (
    AgentDecision,
    ClassificationResult,
    ResponseSuggestion,
    Sentiment,
    TicketCategory,
    TicketPriority,
)

AUTH = {"X-API-Key": "test-key-123"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TENANT_NAME", "test")
    # Force the KV-managed (auto-generated) key path so rotation works in tests
    # regardless of what the developer's .env contains.
    monkeypatch.setattr(settings, "WIDGET_KEY", None, raising=True)
    monkeypatch.setattr(settings, "CHAT_RATE_LIMIT_PER_MINUTE", 100, raising=True)
    settings.DB_PATH = str(tmp_path / "test_chat.db")
    from agent.storage import store as _store

    _store.db_path = settings.DB_PATH
    settings.REQUIRE_API_KEY = True
    settings.API_KEY = SecretStr("test-key-123")
    rate_limit_module._request_log.clear()

    import importlib

    import api.customer_support as cs_module
    import api.main as main_module

    importlib.reload(cs_module)
    importlib.reload(main_module)
    from tests.conftest import FakeClassifier, FakeResponseEngine, FakeShopify

    cs_module._agent.classifier = FakeClassifier()
    cs_module._agent.response_engine = FakeResponseEngine()
    cs_module._agent.shopify = FakeShopify()

    with TestClient(main_module.app, raise_server_exceptions=False) as c:
        yield c, cs_module


def _key(c) -> str:
    """Fetch the auto-generated publishable widget key (as a real widget would)."""
    r = c.get("/support/widget", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()["key"]


def _decision(
    ticket_id: str,
    confidence: float = 0.95,
    requires_human_review: bool = False,
    category: TicketCategory = TicketCategory.ORDER_STATUS,
    action=None,
) -> AgentDecision:
    return AgentDecision(
        ticket_id=ticket_id,
        classification=ClassificationResult(
            category=category,
            priority=TicketPriority.NORMAL,
            sentiment=Sentiment.NEUTRAL,
            reasoning="test",
        ),
        suggestion=ResponseSuggestion(
            ticket_id=ticket_id,
            suggested_response="Your order shipped today!",
            confidence=confidence,
            reasoning="test",
            requires_human_review=requires_human_review,
            suggested_action=action,
        ),
        order_context_used=False,
        auto_sent=False,
    )


def _install_fake_stream(cs_module, confidence=0.95, category=TicketCategory.ORDER_STATUS):
    """Replace handle_ticket_stream with a deterministic event sequence that also
    persists the ticket (mimicking the graph's save_results node)."""
    from agent.storage import store as global_store

    async def fake_stream(ticket):
        await global_store.save(ticket, None)
        await global_store.add_message(ticket.id, "customer", ticket.body)
        await global_store.add_message(ticket.id, "ai", "Your order shipped today!")
        yield {"type": "stage", "stage": "classify_ticket"}
        yield {"type": "stage", "stage": "generate_response"}
        yield {"type": "message", "decision": _decision(ticket.id, confidence, category=category)}

    async def fake_followup(ticket_id, message_body):
        yield {"type": "stage", "stage": "generate_response"}
        yield {"type": "message", "decision": _decision(ticket_id, confidence, category=category)}

    cs_module._agent.handle_ticket_stream = fake_stream
    cs_module._agent.handle_followup_stream = fake_followup


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        lines = block.strip().split("\n")
        if len(lines) >= 2 and lines[0].startswith("event: ") and lines[1].startswith("data: "):
            events.append((lines[0][7:], json.loads(lines[1][6:])))
    return events


# ── Auth & static ─────────────────────────────────────────────


def test_config_rejects_missing_key(client):
    c, _ = client
    r = c.get("/chat/config")
    assert r.status_code == 401
    assert r.json()["error"] == "INVALID_WIDGET_KEY"


def test_config_rejects_wrong_key(client):
    c, _ = client
    assert c.get("/chat/config", headers={"X-Widget-Key": "wrong"}).status_code == 401
    assert c.get("/chat/config?key=wrong").status_code == 401


def test_config_accepts_valid_key_via_header_and_query(client):
    c, _ = client
    key = _key(c)
    r = c.get("/chat/config", headers={"X-Widget-Key": key})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["title"] and body["greeting"]
    assert c.get(f"/chat/config?key={key}").status_code == 200


def test_widget_js_serves_built_script(client):
    c, _ = client
    r = c.get("/chat/widget.js")
    assert r.status_code == 200
    assert len(r.content) > 1000
    assert "javascript" in r.headers["content-type"]


# ── Sessions ──────────────────────────────────────────────────


def test_create_session_and_resume(client):
    c, _ = client
    key = _key(c)
    h = {"X-Widget-Key": key}
    r = c.post("/chat/sessions", headers=h, json={"email": "shopper@example.com", "name": "Sam"})
    assert r.status_code == 200
    session = r.json()
    assert session["session_id"].startswith("cs_")
    assert session["history"] == []

    resumed = c.post("/chat/sessions", headers=h, json={"session_id": session["session_id"]})
    assert resumed.status_code == 200
    assert resumed.json()["session_id"] == session["session_id"]

    fetched = c.get(f"/chat/sessions/{session['session_id']}", headers=h)
    assert fetched.status_code == 200
    assert fetched.json()["session_id"] == session["session_id"]


def test_create_session_rejects_bad_email(client):
    c, _ = client
    key = _key(c)
    r = c.post("/chat/sessions", headers={"X-Widget-Key": key}, json={"email": "not-an-email"})
    assert r.status_code == 422
    assert r.json()["error"] == "VALIDATION_ERROR"


def test_get_unknown_session_404(client):
    c, _ = client
    key = _key(c)
    r = c.get("/chat/sessions/cs_does_not_exist", headers={"X-Widget-Key": key})
    assert r.status_code == 404
    assert r.json()["error"] == "CHAT_SESSION_NOT_FOUND"


# ── Message streaming ─────────────────────────────────────────


def test_send_message_streams_stages_message_and_done(client):
    c, cs_module = client
    _install_fake_stream(cs_module)
    key = _key(c)
    h = {"X-Widget-Key": key}
    session_id = c.post("/chat/sessions", headers=h, json={}).json()["session_id"]

    r = c.post(
        f"/chat/sessions/{session_id}/messages",
        headers=h,
        json={"body": "Where is my order #1001?"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(r.text)
    names = [e for e, _ in events]
    assert names == ["stage", "stage", "message", "done"]
    assert events[0][1]["stage"] == "classify_ticket"
    assert events[0][1]["label"]  # human-readable label shipped alongside

    payload = events[2][1]
    assert payload["role"] == "assistant"
    assert payload["content"] == "Your order shipped today!"
    assert payload["confidence"] == 0.95
    assert payload["needs_human"] is False  # 0.95 clears the 0.88 order_status floor
    assert payload["show_confidence"] is False
    assert events[3][1]["ticket_id"] == payload["ticket_id"]

    # Session is now linked to the ticket and history round-trips.
    sess = c.get(f"/chat/sessions/{session_id}", headers=h).json()
    assert sess["ticket_id"] == payload["ticket_id"]
    assert [m["role"] for m in sess["history"]] == ["customer", "assistant"]


def test_low_confidence_reply_flags_needs_human(client):
    c, cs_module = client
    _install_fake_stream(cs_module, confidence=0.4)
    key = _key(c)
    h = {"X-Widget-Key": key}
    session_id = c.post("/chat/sessions", headers=h, json={}).json()["session_id"]
    r = c.post(f"/chat/sessions/{session_id}/messages", headers=h, json={"body": "hello"})
    payload = next(d for e, d in _parse_sse(r.text) if e == "message")
    assert payload["needs_human"] is True


def test_blocked_category_flags_needs_human_despite_confidence(client):
    c, cs_module = client
    _install_fake_stream(cs_module, confidence=0.99, category=TicketCategory.REFUND)
    key = _key(c)
    h = {"X-Widget-Key": key}
    session_id = c.post("/chat/sessions", headers=h, json={}).json()["session_id"]
    r = c.post(f"/chat/sessions/{session_id}/messages", headers=h, json={"body": "I want a refund"})
    payload = next(d for e, d in _parse_sse(r.text) if e == "message")
    assert payload["needs_human"] is True
    assert payload["category"] == "refund"


def test_second_message_uses_followup_stream(client):
    c, cs_module = client
    _install_fake_stream(cs_module)
    key = _key(c)
    h = {"X-Widget-Key": key}
    session_id = c.post("/chat/sessions", headers=h, json={}).json()["session_id"]
    first = c.post(f"/chat/sessions/{session_id}/messages", headers=h, json={"body": "first"})
    ticket_id = next(d for e, d in _parse_sse(first.text) if e == "message")["ticket_id"]

    calls = []
    original = cs_module._agent.handle_followup_stream

    async def spy(ticket_id_arg, body):
        calls.append((ticket_id_arg, body))
        async for ev in original(ticket_id_arg, body):
            yield ev

    cs_module._agent.handle_followup_stream = spy
    second = c.post(f"/chat/sessions/{session_id}/messages", headers=h, json={"body": "and again"})
    assert second.status_code == 200
    assert calls and calls[0] == (ticket_id, "and again")


def test_stream_failure_degrades_to_error_event(client):
    c, cs_module = client

    async def exploding(ticket):
        yield {"type": "stage", "stage": "classify_ticket"}
        raise RuntimeError("LLM exploded")

    cs_module._agent.handle_ticket_stream = exploding
    key = _key(c)
    h = {"X-Widget-Key": key}
    session_id = c.post("/chat/sessions", headers=h, json={}).json()["session_id"]
    r = c.post(f"/chat/sessions/{session_id}/messages", headers=h, json={"body": "hi"})
    assert r.status_code == 200  # storefront never sees a 500 mid-stream
    events = _parse_sse(r.text)
    assert events[-1][0] == "error"
    assert events[-1][1]["error"] == "STREAM_FAILED"


def test_send_message_unknown_session_404(client):
    c, _ = client
    key = _key(c)
    r = c.post(
        "/chat/sessions/cs_missing/messages", headers={"X-Widget-Key": key}, json={"body": "hi"}
    )
    assert r.status_code == 404


def test_send_message_rejects_whitespace_only_body(client):
    c, _ = client
    key = _key(c)
    h = {"X-Widget-Key": key}
    session_id = c.post("/chat/sessions", headers=h, json={}).json()["session_id"]
    r = c.post(f"/chat/sessions/{session_id}/messages", headers=h, json={"body": "   "})
    assert r.status_code == 422
    assert r.json()["error"] == "VALIDATION_ERROR"


def test_send_message_requires_widget_key(client):
    c, _ = client
    r = c.post("/chat/sessions/cs_x/messages", json={"body": "hi"})
    assert r.status_code == 401


# ── Disabled widget ───────────────────────────────────────────


def test_disabled_widget_blocks_messages_but_reports_state(client):
    c, cs_module = client
    _install_fake_stream(cs_module)
    key = _key(c)
    h = {"X-Widget-Key": key}
    assert c.put("/support/widget", headers=AUTH, json={"enabled": False}).status_code == 200

    cfg = c.get("/chat/config", headers=h).json()
    assert cfg["enabled"] is False  # widget reads this and hides itself

    session_id = c.post("/chat/sessions", headers=h, json={}).json()["session_id"]
    r = c.post(f"/chat/sessions/{session_id}/messages", headers=h, json={"body": "hi"})
    assert r.status_code == 403
    assert r.json()["error"] == "WIDGET_DISABLED"


# ── Handoff ───────────────────────────────────────────────────


async def test_handoff_flags_ticket_with_context(client):
    c, cs_module = client
    _install_fake_stream(cs_module)
    key = _key(c)
    h = {"X-Widget-Key": key}
    session_id = c.post("/chat/sessions", headers=h, json={}).json()["session_id"]
    first = c.post(f"/chat/sessions/{session_id}/messages", headers=h, json={"body": "help me"})
    ticket_id = next(d for e, d in _parse_sse(first.text) if e == "message")["ticket_id"]

    r = c.post(
        f"/chat/sessions/{session_id}/handoff", headers=h, json={"reason": "frustrated customer"}
    )
    assert r.status_code == 200
    assert r.json() == {"ticket_id": ticket_id, "status": "handoff_requested"}

    from agent.storage import store

    row = await store.get(ticket_id)
    meta = row["ticket"]["metadata"]
    assert meta["widget_handoff"] is True
    assert meta["widget_handoff_reason"] == "frustrated customer"
    assert meta["widget_handoff_at"]


def test_handoff_without_ticket_409(client):
    c, _ = client
    key = _key(c)
    h = {"X-Widget-Key": key}
    session_id = c.post("/chat/sessions", headers=h, json={}).json()["session_id"]
    r = c.post(f"/chat/sessions/{session_id}/handoff", headers=h, json={})
    assert r.status_code == 409
    assert r.json()["error"] == "NO_TICKET_YET"


def test_handoff_unknown_session_404(client):
    c, _ = client
    key = _key(c)
    r = c.post("/chat/sessions/cs_nope/handoff", headers={"X-Widget-Key": key}, json={})
    assert r.status_code == 404


# ── Rate limiting ─────────────────────────────────────────────


def test_chat_endpoints_rate_limited_per_ip(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(settings, "CHAT_RATE_LIMIT_PER_MINUTE", 3, raising=True)
    key = _key(c)
    h = {"X-Widget-Key": key}
    # The rate-limit log is keyed by IP across all limiters — clear so only this
    # loop's calls count toward the limit of 3.
    rate_limit_module._request_log.clear()
    codes = [c.get("/chat/config", headers=h).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert 429 in codes
