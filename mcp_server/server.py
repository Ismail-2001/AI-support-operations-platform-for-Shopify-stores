"""
MCP server entrypoint for Customer Support AI Employee.

Exposes read-only tools for querying Shopify orders, knowledge base, and support tickets.
Uses stdio transport for Claude Desktop/Claude Code compatibility.

This implementation uses direct JSON-RPC over stdio to avoid dependency conflicts
with the existing fastapi/starlette versions.
"""

import asyncio
import json
import sys
from typing import Any

import structlog

from agent.config import settings
from agent.knowledge_base import KnowledgeBase
from agent.storage import TicketStore
from integrations.shopify import ShopifyClient, ShopifyNotConfigured

logger = structlog.get_logger(__name__)

# Initialize components (lazy initialization to avoid blocking startup)
_shopify_client: ShopifyClient | None = None
_knowledge_base: KnowledgeBase | None = None
_ticket_store: TicketStore | None = None


def _get_shopify_client() -> ShopifyClient:
    """Get or create Shopify client instance."""
    global _shopify_client
    if _shopify_client is None:
        _shopify_client = ShopifyClient()
    return _shopify_client


def _get_knowledge_base() -> KnowledgeBase:
    """Get or create KnowledgeBase instance."""
    global _knowledge_base
    if _knowledge_base is None:
        _knowledge_base = KnowledgeBase()
    return _knowledge_base


def _get_ticket_store() -> TicketStore:
    """Get or create TicketStore instance."""
    global _ticket_store
    if _ticket_store is None:
        _ticket_store = TicketStore()
    return _ticket_store


# Tool definitions
TOOLS = [
    {
        "name": "lookup_order",
        "description": "Look up a Shopify order by its order number and return a human-readable summary. This tool retrieves real order data from Shopify including fulfillment status, payment status, items ordered, total amount, and tracking information if available.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_number": {
                    "type": "string",
                    "description": "The order number (e.g., '1042', '#1042', or 'ORD-1042')",
                }
            },
            "required": ["order_number"],
        },
    },
    {
        "name": "search_knowledge_base",
        "description": "Search the knowledge base for relevant policy, FAQ, or product information. This tool performs semantic search over the ingested knowledge base content, which includes store policies, FAQs, and product specifications.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query (natural language question or topic)",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return (default: 3, max: 10)",
                    "default": 3,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_ticket",
        "description": "Retrieve a support ticket by its ID, including customer details and AI suggestion. This tool fetches the full ticket record including customer information, classification (category, priority, sentiment), and any AI-generated response suggestion.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {
                    "type": "string",
                    "description": "The unique ticket identifier (UUID format)",
                }
            },
            "required": ["ticket_id"],
        },
    },
    {
        "name": "list_open_tickets",
        "description": "List open and in-progress support tickets, most recent first. This tool returns a list of tickets that are currently open or in progress, ordered by creation date (newest first).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of tickets to return (default: 20, max: 50)",
                    "default": 20,
                }
            },
        },
    },
]


async def lookup_order(order_number: str) -> str:
    """Look up a Shopify order by its order number."""
    try:
        client = _get_shopify_client()
        order = await client.get_order_by_number(order_number)
        if order is None:
            return f"Order {order_number} not found"
        return ShopifyClient.summarize_order(order)
    except ShopifyNotConfigured:
        return "Shopify is not configured. Please set SHOPIFY_SHOP_DOMAIN and SHOPIFY_ACCESS_TOKEN in .env"
    except Exception as e:
        logger.error("mcp_lookup_order_error", order_number=order_number, error=str(e))
        return f"Error looking up order: {e!s}"


async def search_knowledge_base(query: str, top_k: int = 3) -> str:
    """Search the knowledge base."""
    try:
        if top_k > 10:
            top_k = 10
        kb = _get_knowledge_base()
        results = await kb.search(query, top_k=top_k)
        if not results:
            return "No results found. The knowledge base may be empty or no content matches your query."

        formatted = []
        for i, result in enumerate(results, 1):
            formatted.append(
                f"{i}. {result.title} (score: {result.score:.2f}):\n"
                f"   {result.content}\n"
                f"   Source: {result.source}"
            )
        return "\n\n".join(formatted)
    except Exception as e:
        logger.error("mcp_kb_search_error", query=query, error=str(e))
        return f"Error searching knowledge base: {e!s}"


async def get_ticket(ticket_id: str) -> str:
    """Retrieve a support ticket by its ID."""
    try:
        store = _get_ticket_store()
        result = await store.get(ticket_id)
        if result is None:
            return f"Ticket {ticket_id} not found"

        ticket = result["ticket"]
        suggestion = result.get("suggestion")

        lines = [
            f"Ticket {ticket_id}",
            f"Customer: {ticket.get('customer_email')}",
            f"Subject: {ticket.get('subject')}",
            f"Category: {ticket.get('category')} | Priority: {ticket.get('priority')} | Status: {ticket.get('status')}",
            f"Created: {ticket.get('created_at')}",
        ]

        if suggestion:
            lines.append(f"\nAI Suggestion:\n{suggestion}")
        else:
            lines.append("\nNo AI suggestion available for this ticket.")

        return "\n".join(lines)
    except Exception as e:
        logger.error("mcp_get_ticket_error", ticket_id=ticket_id, error=str(e))
        return f"Error retrieving ticket: {e!s}"


async def list_open_tickets(limit: int = 20) -> str:
    """List open and in-progress support tickets."""
    try:
        if limit > 50:
            limit = 50
        store = _get_ticket_store()
        all_tickets = await store.list(limit=limit)

        # Filter to open/in-progress status
        open_tickets = [
            t for t in all_tickets if t["ticket"].get("status") in ("open", "in_progress")
        ]

        if not open_tickets:
            return "No open tickets found"

        formatted = []
        for i, result in enumerate(open_tickets, 1):
            ticket = result["ticket"]
            formatted.append(
                f"{i}. Ticket {ticket.get('id')}\n"
                f"   Customer: {ticket.get('customer_email')} | Subject: {ticket.get('subject')}\n"
                f"   Category: {ticket.get('category')} | Priority: {ticket.get('priority')} | "
                f"Status: {ticket.get('status')}\n"
                f"   Created: {ticket.get('created_at')}"
            )
        return "\n\n".join(formatted)
    except Exception as e:
        logger.error("mcp_list_tickets_error", error=str(e))
        return f"Error listing tickets: {e!s}"


# Tool handler mapping
TOOL_HANDLERS = {
    "lookup_order": lookup_order,
    "search_knowledge_base": search_knowledge_base,
    "get_ticket": get_ticket,
    "list_open_tickets": list_open_tickets,
}


async def handle_tool_call(tool_name: str, arguments: dict[str, Any]) -> str:
    """Handle a tool call from the MCP client."""
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return f"Unknown tool: {tool_name}"

    return await handler(**arguments)


def send_response(response: dict[str, Any]) -> None:
    """Send a JSON-RPC response to stdout."""
    json.dump(response, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


async def handle_request(request: dict[str, Any]) -> None:
    """Handle an incoming JSON-RPC request."""
    method = request.get("method")
    params = request.get("params", {})
    request_id = request.get("id")

    if method == "initialize":
        send_response(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "support-agent", "version": "1.0.0"},
                    "capabilities": {"tools": {}},
                },
            }
        )

    elif method == "tools/list":
        send_response({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})

    elif method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        result = await handle_tool_call(tool_name, arguments)
        send_response(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": result}]},
            }
        )

    else:
        send_response(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        )


async def main():
    """Initialize the MCP server and run with stdio transport."""
    # Initialize database connections
    kb = _get_knowledge_base()
    await kb.init()

    store = _get_ticket_store()
    await store.init()

    logger.info("mcp_server_starting", tenant=settings.TENANT_NAME)

    # Read JSON-RPC requests from stdin
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
            await handle_request(request)
        except json.JSONDecodeError as e:
            logger.error("mcp_json_decode_error", error=str(e))
            send_response(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            )
        except Exception as e:
            logger.error("mcp_request_error", error=str(e))
            send_response(
                {
                    "jsonrpc": "2.0",
                    "id": request.get("id") if isinstance(request, dict) else None,
                    "error": {"code": -32603, "message": f"Internal error: {e!s}"},
                }
            )


if __name__ == "__main__":
    asyncio.run(main())
