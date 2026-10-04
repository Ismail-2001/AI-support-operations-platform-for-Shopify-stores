"""
Customer Support Agent - API entrypoint.
AI-powered customer support automation for ecommerce, MCP-ready, Shopify + Gorgias connected.

One deployed instance serves exactly one client. Do NOT route multiple clients through
the same instance — the tenant name is a deployment-time label, not a row-level filter.
"""

import asyncio
import sys
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agent.config import settings
from agent.knowledge_base import knowledge_base
from agent.storage import storage_is_ephemeral, store
from api.chat import chat_router
from api.customer_support import public_router, webhook_router
from api.customer_support import router as support_router
from api.errors import APIError
from api.middleware import RequestIDMiddleware, RequestLoggingMiddleware, WebhookBodyLimitMiddleware
from api.setup import router as setup_router

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
        if settings.ENV == "production"
        else structlog.dev.ConsoleRenderer(),
    ]
)
logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handles graceful shutdown on SIGTERM/SIGINT.
    Allows in-flight requests to complete before shutting down."""
    # Startup
    structlog.contextvars.bind_contextvars(tenant_name=settings.TENANT_NAME)

    logger.info(
        "tenant_startup",
        tenant_name=settings.TENANT_NAME,
        db_path=settings.DB_PATH,
        shopify_domain=settings.SHOPIFY_SHOP_DOMAIN,
        gorgias_domain=settings.GORGIAS_DOMAIN,
    )

    if settings.REQUIRE_API_KEY and not settings.API_KEY:
        logger.warning(
            "startup_warning_no_api_key",
            message="REQUIRE_API_KEY is true but API_KEY is unset — every protected "
            "request will fail with a clear 500 until you set API_KEY in .env.",
        )
    if not settings.GOOGLE_API_KEY:
        logger.warning(
            "startup_warning_no_google_key",
            message="GOOGLE_API_KEY is not set — Knowledge Base search will not work "
            "until it's configured, even if you're using Groq/OpenRouter for chat. "
            "Embeddings require Google's gemini-embedding-001 regardless of "
            "your chat provider.",
        )
    await store.init()
    await knowledge_base.init()
    logger.info("cs_agent_started", tenant_name=settings.TENANT_NAME)

    # Signal handlers only work on Unix; on Windows, SIGTERM is not supported
    # by add_signal_handler. Uvicorn handles SIGINT natively anyway.
    # When running under TestClient (e.g. pytest), we may not be in the main
    # thread — add_signal_handler raises RuntimeError in that case.  Log and
    # skip graceful-shutdown rather than crashing the entire app.
    if sys.platform != "win32":
        shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        import signal

        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, shutdown_event.set)
        except RuntimeError:
            logger.debug(
                "signal_handler_skipped",
                reason="not in main thread — graceful shutdown via SIGTERM disabled",
            )
        else:
            await shutdown_event.wait()
            logger.info("shutdown_initiated", tenant_name=settings.TENANT_NAME)
            await asyncio.sleep(5)

    yield

    logger.info("shutdown_complete", tenant_name=settings.TENANT_NAME)


app = FastAPI(
    title="Customer Support Agent",
    description="AI-powered customer support automation for ecommerce — Shopify + Gorgias connected.",
    version="2.0.0",
    lifespan=lifespan,
)

# Middleware order matters: outermost runs first.
# RequestIDMiddleware -> RequestLoggingMiddleware -> WebhookBodyLimitMiddleware -> CORSMiddleware
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(WebhookBodyLimitMiddleware)

# CORS: /chat (the storefront widget) must work from any storefront origin — it
# authenticates with the publishable widget key + per-IP rate limit, and the
# private API key still guards every /support/* endpoint from browser callers.
# WIDGET_ALLOWED_ORIGINS tightens this to your storefront(s) if you want
# (regex, default ".*"). ALLOWED_ORIGINS adds explicit dashboard origin(s).
_allowed_origins = [o.strip() for o in settings.ALLOWED_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_origin_regex=settings.WIDGET_ALLOWED_ORIGINS or None,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "PUT"],
    allow_headers=[
        "X-API-Key",
        "Content-Type",
        "Idempotency-Key",
        "X-Webhook-Secret",
        "X-Widget-Key",
        "Accept",
    ],
)

app.include_router(support_router)
app.include_router(setup_router)
app.include_router(webhook_router)
app.include_router(public_router)
app.include_router(chat_router)


@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError):
    """Structured error responses for APIError exceptions."""
    body = {"error": exc.error_code, "message": exc.detail}
    if exc.error_details:
        body["details"] = exc.error_details
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Never leak stack traces / internal details to the client — log the full thing
    # server-side and return a generic message.
    logger.error("unhandled_exception", path=request.url.path, error=str(exc))
    return JSONResponse(
        status_code=500,
        content={"error": "INTERNAL_SERVER_ERROR", "message": "Internal server error"},
    )


@app.get("/health")
async def health():
    """Deep health check: verifies database, knowledge base, and integration status."""
    checks = {}

    # Database check
    try:
        await store.analytics()
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {e}"

    # Knowledge base check
    try:
        chunk_count = await knowledge_base.count()
        checks["knowledge_base"] = f"ok ({chunk_count} chunks)"
    except Exception as e:
        checks["knowledge_base"] = f"error: {e}"

    # Integration status
    from integrations.gorgias import GorgiasClient
    from integrations.shopify import ShopifyClient

    gorgias = GorgiasClient()
    shopify = ShopifyClient()

    checks["shopify"] = "connected" if shopify.enabled else "not_configured"
    checks["gorgias"] = "connected" if gorgias.enabled else "not_configured"
    checks["auto_send"] = "enabled" if settings.AUTO_SEND_ENABLED else "disabled"

    # Storage persistence — "ephemeral" means data is wiped on the next
    # deploy/restart (Render without an attached disk). Informational only:
    # it does not flip overall status, so free-tier demos still read healthy.
    checks["storage"] = "ephemeral" if storage_is_ephemeral() else "persistent"

    overall = "healthy" if checks["database"] == "ok" else "degraded"

    return {"status": overall, "agent": "cs-agent", "checks": checks}


@app.get("/")
async def root():
    return {
        "agent": "Customer Support Agent",
        "description": "AI-powered customer support automation for ecommerce",
        "version": "2.0.0",
        "docs": "/docs",
        "health": "/health",
        "support_health": "/support/health",
    }
