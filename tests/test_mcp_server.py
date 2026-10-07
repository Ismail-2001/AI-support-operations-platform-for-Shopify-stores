"""
Tests for MCP server - read-only tools only.

All tests use fake Shopify/KB/storage to avoid real network calls.
Tests verify:
1. Each tool works correctly with fake data
2. NO write/mutating tools are exposed (safety regression test)
3. Graceful handling of missing data
"""

import os
import sys
from typing import ClassVar

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.knowledge_base import KnowledgeChunk
from integrations.shopify import ShopifyNotConfigured
from mcp_server.server import TOOL_HANDLERS, TOOLS


class FakeShopifyForMCP:
    """Fake Shopify client for MCP tests."""

    enabled = True

    async def get_order_by_number(self, order_number):
        if order_number == "999":
            return None  # Not found case
        return {
            "name": f"#{order_number}",
            "created_at": "2024-01-15T10:30:00Z",
            "fulfillment_status": "fulfilled",
            "financial_status": "paid",
            "total_price": "59.00",
            "currency": "USD",
            "line_items": [{"title": "Basic Hoodie", "quantity": 2}],
            "fulfillments": [{"tracking_number": "1Z999AA10123456784", "tracking_company": "UPS"}],
        }


class FakeKBForMCP:
    """Fake knowledge base for MCP tests."""

    async def search(self, query, top_k=3, min_score=0.55):
        if query == "empty":
            return []
        all_results = [
            KnowledgeChunk(
                id=1,
                source="faq",
                title="Return Policy",
                content="Customers can return items within 30 days.",
                score=0.92,
            ),
            KnowledgeChunk(
                id=2,
                source="faq",
                title="Shipping Info",
                content="We ship worldwide within 3-5 business days.",
                score=0.85,
            ),
        ]
        return all_results[:top_k]


class FakeStoreForMCP:
    """Fake ticket store for MCP tests."""

    def __init__(self):
        self.tickets = {
            "ticket-123": {
                "ticket": {
                    "id": "ticket-123",
                    "customer_email": "customer@example.com",
                    "subject": "Order question",
                    "category": "order_status",
                    "priority": "normal",
                    "status": "open",
                    "created_at": "2024-01-15T10:30:00Z",
                },
                "suggestion": {
                    "suggested_response": "Your order has been shipped.",
                    "confidence": 0.9,
                },
            },
            "ticket-456": {
                "ticket": {
                    "id": "ticket-456",
                    "customer_email": "other@example.com",
                    "subject": "Return request",
                    "category": "return",
                    "priority": "high",
                    "status": "in_progress",
                    "created_at": "2024-01-16T14:20:00Z",
                },
                "suggestion": None,
            },
        }

    async def get(self, ticket_id):
        return self.tickets.get(ticket_id)

    async def list(self, limit=20, **kwargs):
        return list(self.tickets.values())[:limit]


@pytest.fixture
def fake_mcp_components(monkeypatch):
    """Inject fake components for MCP tests."""
    monkeypatch.setattr("mcp_server.server._shopify_client", FakeShopifyForMCP())
    monkeypatch.setattr("mcp_server.server._knowledge_base", FakeKBForMCP())
    monkeypatch.setattr("mcp_server.server._ticket_store", FakeStoreForMCP())


class TestLookupOrder:
    """Tests for lookup_order tool."""

    @pytest.mark.asyncio
    async def test_lookup_order_success(self, fake_mcp_components):
        """Should return formatted order summary for existing order."""
        from mcp_server.server import lookup_order

        result = await lookup_order("1042")
        assert "Order #1042" in result
        assert "fulfilled" in result
        assert "paid" in result
        assert "Basic Hoodie" in result
        assert "1Z999AA10123456784" in result

    @pytest.mark.asyncio
    async def test_lookup_order_not_found(self, fake_mcp_components):
        """Should return 'not found' message for nonexistent order."""
        from mcp_server.server import lookup_order

        result = await lookup_order("999")
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_lookup_order_shopify_not_configured(self, monkeypatch):
        """Should handle ShopifyNotConfigured gracefully."""
        monkeypatch.setattr("mcp_server.server._shopify_client", None)
        from mcp_server.server import lookup_order

        # Mock the client to raise ShopifyNotConfigured
        class NotConfiguredShopify:
            enabled = False

            async def get_order_by_number(self, order_number):
                raise ShopifyNotConfigured("Not configured")

        monkeypatch.setattr("mcp_server.server._shopify_client", NotConfiguredShopify())

        result = await lookup_order("1042")
        assert "not configured" in result.lower()


class TestSearchKnowledgeBase:
    """Tests for search_knowledge_base tool."""

    @pytest.mark.asyncio
    async def test_search_kb_success(self, fake_mcp_components):
        """Should return formatted search results."""
        from mcp_server.server import search_knowledge_base

        result = await search_knowledge_base("return policy")
        assert "Return Policy" in result
        assert "0.92" in result  # Score
        assert "30 days" in result
        assert "Shipping Info" in result

    @pytest.mark.asyncio
    async def test_search_kb_empty_results(self, fake_mcp_components):
        """Should return 'no results' message for empty KB."""
        from mcp_server.server import search_knowledge_base

        result = await search_knowledge_base("empty")
        assert "no results" in result.lower()

    @pytest.mark.asyncio
    async def test_search_kb_respects_limit(self, fake_mcp_components):
        """Should respect top_k parameter."""
        from mcp_server.server import search_knowledge_base

        result = await search_knowledge_base("return policy", top_k=1)
        # Should only show first result
        assert "Return Policy" in result
        assert "Shipping Info" not in result


class TestGetTicket:
    """Tests for get_ticket tool."""

    @pytest.mark.asyncio
    async def test_get_ticket_success(self, fake_mcp_components):
        """Should return formatted ticket with suggestion."""
        from mcp_server.server import get_ticket

        result = await get_ticket("ticket-123")
        assert "ticket-123" in result
        assert "customer@example.com" in result
        assert "Order question" in result
        assert "order_status" in result
        assert "Your order has been shipped" in result

    @pytest.mark.asyncio
    async def test_get_ticket_not_found(self, fake_mcp_components):
        """Should return 'not found' message for nonexistent ticket."""
        from mcp_server.server import get_ticket

        result = await get_ticket("nonexistent")
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_get_ticket_no_suggestion(self, fake_mcp_components):
        """Should handle tickets without AI suggestions."""
        from mcp_server.server import get_ticket

        result = await get_ticket("ticket-456")
        assert "ticket-456" in result
        assert "No AI suggestion available" in result


class TestListOpenTickets:
    """Tests for list_open_tickets tool."""

    @pytest.mark.asyncio
    async def test_list_open_tickets_success(self, fake_mcp_components):
        """Should return formatted list of open tickets."""
        from mcp_server.server import list_open_tickets

        result = await list_open_tickets(10)
        assert "ticket-123" in result
        assert "ticket-456" in result
        assert "customer@example.com" in result
        assert "other@example.com" in result

    @pytest.mark.asyncio
    async def test_list_open_tickets_empty(self, monkeypatch):
        """Should return 'no open tickets' when none exist."""

        class EmptyStore:
            async def list(self, limit=20, **kwargs):
                return []

        monkeypatch.setattr("mcp_server.server._ticket_store", EmptyStore())
        from mcp_server.server import list_open_tickets

        result = await list_open_tickets(10)
        assert "no open tickets" in result.lower()

    @pytest.mark.asyncio
    async def test_list_open_tickets_respects_limit(self, fake_mcp_components):
        """Should respect limit parameter."""
        from mcp_server.server import list_open_tickets

        result = await list_open_tickets(1)
        # Should only show first ticket
        lines = result.split("\n\n")
        assert len(lines) == 1  # Only one ticket


class TestNoWriteToolsSafety:
    """Safety regression test - ensure NO write/mutating tools are exposed."""

    WRITE_KEYWORDS: ClassVar[list[str]] = [
        "refund",
        "approve",
        "create",
        "update",
        "delete",
        "send",
        "modify",
        "change",
        "execute",
        "mutate",
        "write",
    ]

    def test_no_write_tools_in_registered_list(self):
        """Introspect registered tools and assert none contain write keywords."""
        # Get all registered tools from the TOOLS list
        tool_names = [tool["name"] for tool in TOOLS]
        tool_descriptions = [tool["description"] for tool in TOOLS]

        # Check tool names
        for name in tool_names:
            for keyword in self.WRITE_KEYWORDS:
                assert keyword not in name.lower(), (
                    f"Tool name '{name}' contains write keyword '{keyword}'. "
                    f"Write operations must NOT be exposed via MCP."
                )

        # Check tool descriptions
        for desc in tool_descriptions:
            for keyword in self.WRITE_KEYWORDS:
                # Allow "created_at" and similar passive references
                if keyword in ["create", "update"] and "created_at" in desc.lower():
                    continue
                assert keyword not in desc.lower(), (
                    f"Tool description contains write keyword '{keyword}': {desc}. "
                    f"Write operations must NOT be exposed via MCP."
                )

        # Verify we have exactly the expected read-only tools
        expected_tools = {
            "lookup_order",
            "search_knowledge_base",
            "get_ticket",
            "list_open_tickets",
        }
        assert set(tool_names) == expected_tools, (
            f"Expected tools {expected_tools}, but found {set(tool_names)}. "
            f"Only read-only tools should be registered."
        )

    def test_expected_read_only_tools_present(self):
        """Verify all expected read-only tools are registered."""
        tool_names = {tool["name"] for tool in TOOLS}

        expected = {"lookup_order", "search_knowledge_base", "get_ticket", "list_open_tickets"}
        assert expected.issubset(tool_names), (
            f"Missing expected read-only tools. Expected {expected}, found {tool_names}"
        )

    def test_tool_handlers_match_tool_list(self):
        """Verify TOOL_HANDLERS dict matches TOOLS list."""
        tool_names = {tool["name"] for tool in TOOLS}
        handler_names = set(TOOL_HANDLERS.keys())

        assert tool_names == handler_names, (
            f"Tool names ({tool_names}) don't match handler names ({handler_names})"
        )
