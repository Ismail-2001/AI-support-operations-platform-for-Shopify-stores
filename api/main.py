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
from agent.storage import store
from api.customer_support import public_router, webhook_router
from api.customer_support import router as support_router

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
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
    await store.init()
    await knowledge_base.init()
    logger.info("cs_agent_started", tenant_name=settings.TENANT_NAME)

    # Signal handlers only work on Unix; on Windows, SIGTERM is not supported
    # by add_signal_handler. Uvicorn handles SIGINT natively anyway.
    if sys.platform != "win32":
        shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        import signal

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, shutdown_event.set)
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

# CORS: only your dashboard's own domain(s) should be allowed to call this from a browser.
# Server-to-server calls (Gorgias webhooks, your own backend) are unaffected by CORS —
# this only restricts what a webpage's JavaScript is allowed to do.
_allowed_origins = [o.strip() for o in settings.ALLOWED_ORIGINS.split(",") if o.strip()]
if _allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["X-API-Key", "Content-Type", "Idempotency-Key", "X-Webhook-Secret"],
    )

app.include_router(support_router)
app.include_router(webhook_router)
app.include_router(public_router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Never leak stack traces / internal details to the client — log the full thing
    # server-side and return a generic message.
    logger.error("unhandled_exception", path=request.url.path, error=str(exc))
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
async def health():
    return {"status": "healthy", "agent": "cs-agent"}


@app.get("/")
async def root():
    return {
        "agent": "Customer Support Agent",
        "description": "AI-powered customer support automation for ecommerce",
        "docs": "/docs",
        "health": "/health",
        "support_health": "/support/health",
    }
