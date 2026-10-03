"""Tests for LLM fallback logic — invoke_with_fallback and integration
with classifier/response_engine.  Uses AsyncMock to simulate provider failures
so no real network calls are made."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from agent.config import settings
from agent.llm import invoke_with_fallback
from agent.models import (
    ClassificationResult,
    Sentiment,
    TicketCategory,
    TicketPriority,
)

# ── invoke_with_fallback unit tests ─────────────────────────


async def test_fallback_gemini_success_not_called():
    """Gemini succeeds -> fallback never called."""
    primary = AsyncMock()
    primary.ainvoke.return_value = {"parsed": "ok", "raw": None}
    fallback = AsyncMock()

    result, model = await invoke_with_fallback(
        primary,
        fallback,
        ["msg"],
        primary_model_name="gemini-2.0-flash",
        fallback_model_name="claude-haiku",
    )

    assert result["parsed"] == "ok"
    assert model == "gemini-2.0-flash"
    primary.ainvoke.assert_awaited_once()
    fallback.ainvoke.assert_not_awaited()


async def test_fallback_gemini_fails_fallback_succeeds():
    """Gemini fails (non-retryable error), fallback configured -> fallback is called."""
    primary = AsyncMock()
    primary.ainvoke = AsyncMock(side_effect=ValueError("invalid API key"))
    fallback = AsyncMock()
    fallback.ainvoke.return_value = {"parsed": "fallback_ok", "raw": None}

    result, model = await invoke_with_fallback(
        primary,
        fallback,
        ["msg"],
        primary_model_name="gemini-2.0-flash",
        fallback_model_name="claude-haiku",
    )

    assert result["parsed"] == "fallback_ok"
    assert model == "claude-haiku"
    assert primary.ainvoke.await_count == 1  # non-retryable -> no retries
    fallback.ainvoke.assert_awaited_once()


async def test_fallback_gemini_fails_no_fallback_raises():
    """Gemini fails (non-retryable), no fallback -> original exception propagates immediately."""
    primary = AsyncMock()
    primary.ainvoke = AsyncMock(side_effect=ValueError("API key invalid"))

    with pytest.raises(ValueError, match="API key invalid"):
        await invoke_with_fallback(
            primary,
            None,
            ["msg"],
            primary_model_name="gemini-2.0-flash",
            fallback_model_name="claude-haiku",
        )

    assert primary.ainvoke.await_count == 1  # non-retryable -> no retries


# ── Integration: classifier/response_engine model name in traces ──


async def test_fallback_classifier_records_fallback_model_name(monkeypatch):
    """When fallback answers, classifier traces show the fallback model name."""
    monkeypatch.setattr(settings, "GOOGLE_API_KEY", SecretStr("dummy"))
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", SecretStr("sk-ant-dummy"))

    from agent.classifier import TicketClassifier

    classifier = TicketClassifier()

    mock_parsed = MagicMock(
        spec=ClassificationResult,
        category=TicketCategory.ORDER_STATUS,
        priority=TicketPriority.NORMAL,
        sentiment=Sentiment.NEUTRAL,
        extracted_order_number=None,
        reasoning="test",
    )
    mock_parsed.model_dump.return_value = {
        "category": "order_status",
        "priority": "normal",
        "sentiment": "neutral",
        "reasoning": "test",
    }

    classifier.llm = MagicMock()
    classifier.llm.ainvoke = AsyncMock(side_effect=[ValueError("down"), ValueError("down")])
    classifier.fallback_llm = MagicMock()
    classifier.fallback_llm.ainvoke = AsyncMock(return_value={"parsed": mock_parsed, "raw": None})

    record_mock = AsyncMock(return_value=0.0)
    monkeypatch.setattr("agent.classifier.record_llm_call", record_mock)

    from agent.models import SupportTicket

    ticket = SupportTicket(id="fb-cls", customer_email="a@b.com", subject="hi", body="hello")
    await classifier.classify(ticket)

    assert record_mock.await_count == 1
    call_kwargs = record_mock.call_args.kwargs
    assert call_kwargs["model"] == settings.FALLBACK_MODEL


async def test_fallback_classifier_records_primary_model_name(monkeypatch):
    """When the primary model succeeds on first try, traces show the primary model name."""
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", SecretStr("sk-or-dummy"))
    monkeypatch.setattr(settings, "GROQ_API_KEY", None)
    monkeypatch.setattr(settings, "GOOGLE_API_KEY", None)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", SecretStr("sk-ant-dummy"))

    from agent.classifier import TicketClassifier

    classifier = TicketClassifier()

    mock_parsed = MagicMock(
        spec=ClassificationResult,
        category=TicketCategory.ORDER_STATUS,
        priority=TicketPriority.NORMAL,
        sentiment=Sentiment.NEUTRAL,
        extracted_order_number=None,
        reasoning="test",
    )
    mock_parsed.model_dump.return_value = {
        "category": "order_status",
        "priority": "normal",
        "sentiment": "neutral",
        "reasoning": "test",
    }

    classifier.llm = MagicMock()
    classifier.llm.ainvoke = AsyncMock(return_value={"parsed": mock_parsed, "raw": None})
    classifier.fallback_llm = AsyncMock()

    record_mock = AsyncMock(return_value=0.0)
    monkeypatch.setattr("agent.classifier.record_llm_call", record_mock)

    from agent.models import SupportTicket

    ticket = SupportTicket(id="fb-cls-pri", customer_email="a@b.com", subject="hi", body="hello")
    await classifier.classify(ticket)

    assert record_mock.await_count == 1
    call_kwargs = record_mock.call_args.kwargs
    assert call_kwargs["model"] == settings.OPENROUTER_MODEL
    classifier.fallback_llm.ainvoke.assert_not_awaited()
