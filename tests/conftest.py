"""
Shared fixtures. Everything here avoids real network calls (no Gemini, no Shopify, no
Gorgias) — these are unit/integration tests of OUR orchestration logic, not of Google's
or Shopify's APIs. Each test gets its own temp SQLite file so tests can't interfere
with each other.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# TENANT_NAME is required by config.Settings with no default.  Set a test default
# here so every test environment has it — individual tests can override via
# monkeypatch.setenv("TENANT_NAME", ...) when needed.
os.environ.setdefault("TENANT_NAME", "test")

from agent.config import settings
from agent.knowledge_base import KnowledgeBase
from agent.models import (
    ClassificationResult,
    ResponseSuggestion,
    Sentiment,
    TicketCategory,
    TicketPriority,
)
from agent.storage import TicketStore


@pytest.fixture
def temp_db_path(tmp_path):
    return str(tmp_path / f"test_{uuid.uuid4().hex}.db")


@pytest.fixture
async def test_store(temp_db_path):
    store = TicketStore(db_path=temp_db_path)
    await store.init()
    return store


@pytest.fixture
async def test_kb(temp_db_path):
    kb = KnowledgeBase(db_path=temp_db_path)
    await kb.init()
    return kb


@pytest.fixture(autouse=True)
def reset_settings():
    """Every test starts from known-good defaults so one test's settings mutation
    can't silently affect another test."""
    settings.AUTO_SEND_ENABLED = False
    settings.AUTO_SEND_MIN_CONFIDENCE = 0.85
    settings.AUTO_SEND_BLOCKED_CATEGORIES = "refund,complaint,legal,other,subscription"
    settings.AUTO_SEND_MIN_CONFIDENCE_ORDER_STATUS = 0.88
    settings.AUTO_SEND_MIN_CONFIDENCE_SHIPPING = 0.88
    settings.AUTO_SEND_MIN_CONFIDENCE_PRODUCT_QUESTION = 0.90
    settings.AUTO_SEND_MIN_CONFIDENCE_RETURNS = 0.87
    settings.AUTO_SEND_MIN_CONFIDENCE_TECHNICAL = 0.90
    settings.AUTO_SEND_MIN_CONFIDENCE_DEFAULT = 0.90
    settings.REQUIRE_API_KEY = False
    settings.API_KEY = None
    settings.RATE_LIMIT_PER_MINUTE = 60
    settings.REFUND_RATE_LIMIT_PER_MINUTE = 10
    settings.RESEND_RATE_LIMIT_PER_MINUTE = 10
    settings.ACTION_RATE_LIMIT_PER_MINUTE = 10
    settings.DAILY_COST_CAP_USD = 5.0
    settings.ENV = "development"
    yield


class FakeClassifier:
    """Returns a fixed classification regardless of input, unless overridden per-test."""

    def __init__(
        self,
        category=TicketCategory.ORDER_STATUS,
        priority=TicketPriority.NORMAL,
        sentiment=Sentiment.NEUTRAL,
        extracted_order_number=None,
    ):
        self.category = category
        self.priority = priority
        self.sentiment = sentiment
        self.extracted_order_number = extracted_order_number

    async def classify(self, ticket, history=None):
        return ClassificationResult(
            category=self.category,
            priority=self.priority,
            sentiment=self.sentiment,
            extracted_order_number=self.extracted_order_number,
            reasoning="test",
        )


class FakeResponseEngine:
    def __init__(self, confidence=0.9, requires_human_review=False, suggested_action=None):
        self.confidence = confidence
        self.requires_human_review = requires_human_review
        self.suggested_action = suggested_action

    async def generate_suggestion(
        self,
        ticket,
        classification,
        order_context=None,
        knowledge_context=None,
        subscription_context=None,
        return_context=None,
        history=None,
    ):
        return ResponseSuggestion(
            ticket_id=ticket.id,
            suggested_response="Test response",
            confidence=self.confidence,
            reasoning="test",
            requires_human_review=self.requires_human_review,
            suggested_action=self.suggested_action,
        )


class FakeShopify:
    enabled = False

    async def get_order_by_number(self, order_number):
        return None
