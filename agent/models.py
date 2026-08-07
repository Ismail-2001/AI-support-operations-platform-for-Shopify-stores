from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TicketCategory(str, Enum):
    ORDER_STATUS = "order_status"
    SHIPPING = "shipping"
    RETURNS = "returns"
    REFUND = "refund"
    PRODUCT_QUESTION = "product_question"
    COMPLAINT = "complaint"
    TECHNICAL = "technical"
    OTHER = "other"


class TicketPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"
    CRITICAL = "critical"


class TicketStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    AWAITING_CUSTOMER = "awaiting_customer"
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketChannel(str, Enum):
    EMAIL = "email"
    CHAT = "chat"
    GORGIAS = "gorgias"
    SOCIAL = "social"
    PHONE = "phone"


class Sentiment(str, Enum):
    VERY_NEGATIVE = "very_negative"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    POSITIVE = "positive"
    VERY_POSITIVE = "very_positive"


class SupportTicket(BaseModel):
    id: str
    shop_domain: str | None = None
    customer_email: str
    customer_name: str | None = None
    subject: str
    body: str
    channel: TicketChannel = TicketChannel.EMAIL
    order_id: str | None = None
    order_number: str | None = None
    product_id: str | None = None
    gorgias_ticket_id: str | None = None
    status: TicketStatus = TicketStatus.OPEN
    category: TicketCategory | None = None
    priority: TicketPriority | None = None
    sentiment: Sentiment | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class MessageSender(str, Enum):
    CUSTOMER = "customer"
    AGENT = "agent"  # human agent
    AI = "ai"  # this bot, when it auto-sent


class TicketMessage(BaseModel):
    id: int | None = None
    ticket_id: str
    sender_type: MessageSender
    content: str
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class ClassificationResult(BaseModel):
    category: TicketCategory
    priority: TicketPriority
    sentiment: Sentiment
    extracted_order_number: str | None = Field(
        default=None,
        description="Order number mentioned in the ticket body, e.g. '#1042' or '1042'. Null if none found.",
    )
    reasoning: str = Field(description="One short sentence explaining the classification.")


class ResponseSuggestion(BaseModel):
    ticket_id: str
    suggested_response: str
    confidence: float
    reasoning: str
    requires_human_review: bool
    follow_up_questions: list[str] = Field(default_factory=list)
    suggested_action: SuggestedAction | None = None


class ActionType(str, Enum):
    REFUND = "refund"
    RESEND_ORDER = "resend_order"
    NONE = "none"


class SuggestedAction(BaseModel):
    type: ActionType = ActionType.NONE
    order_id: str | None = None
    amount: float | None = None
    reason: str | None = None
    # Actions are ALWAYS human-approved regardless of response confidence — see
    # api/customer_support.py's /actions/refund endpoint. This flag is informational only.
    requires_approval: bool = True


class KnowledgeChunk(BaseModel):
    id: int | None = None
    source: str  # e.g. "policy:returns", "product:blue-hoodie"
    title: str
    content: str
    score: float | None = None  # similarity score, populated only on search results


class EditRecord(BaseModel):
    ticket_id: str
    ai_suggestion: str
    final_response: str
    was_edited: bool
    similarity: float
    category: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class AgentDecision(BaseModel):
    ticket_id: str
    classification: ClassificationResult
    suggestion: ResponseSuggestion
    order_context_used: bool
    auto_sent: bool


class SupportAnalytics(BaseModel):
    total_tickets: int
    open_tickets: int
    avg_response_time_hours: float | None = None
    avg_resolution_time_hours: float | None = None
    satisfaction_score: float | None = None
    first_contact_resolution_rate: float | None = None
    escalation_rate: float | None = None
    category_breakdown: dict[str, int] = Field(default_factory=dict)
    priority_breakdown: dict[str, int] = Field(default_factory=dict)
    channel_breakdown: dict[str, int] = Field(default_factory=dict)
    sentiment_distribution: dict[str, int] = Field(default_factory=dict)
