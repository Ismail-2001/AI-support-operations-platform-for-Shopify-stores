"""Request middleware: unique request IDs, structured request/response logging,
and API version headers.

Every request gets a unique `X-Request-ID` (UUID4) that is:
- Returned in the response header
- Bound to structlog's contextvars so all log lines within that request include it
- Sent downstream if the client provides one (for distributed tracing)
"""

import time
import uuid
from typing import ClassVar

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

logger = structlog.get_logger(__name__)

_API_VERSION = "2.0.0"

# 1 MB — generous for webhook payloads, but prevents multi-GB memory exhaustion
_MAX_WEBHOOK_BODY_BYTES = 1 * 1024 * 1024


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Assigns a unique request ID to every request and binds it to structlog context."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-API-Version"] = _API_VERSION

        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Logs every request with method, path, status code, and duration.

    Skips health check endpoints to avoid log noise from uptime monitors.
    """

    _SKIP_PATHS: ClassVar[set[str]] = {"/health", "/support/health"}

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in self._SKIP_PATHS:
            return await call_next(request)

        start = time.monotonic()
        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000

        logger.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(duration_ms, 1),
            client_ip=request.client.host if request.client else "unknown",
        )

        return response


class StoreContextMiddleware(BaseHTTPMiddleware):
    """Binds an X-Store-Id header to the current request context.

    Validates the store against the registry (404 STORE_NOT_FOUND otherwise),
    eagerly initializes that store's data-plane objects (TicketStore /
    KnowledgeBase / agent), then sets the ``current_store_id`` ContextVar that
    the module-level ContextProxy singletons resolve through. The var is
    reset when the request finishes; no header means no context, so
    single-store deploys behave exactly as before.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        store_id = request.headers.get("x-store-id")
        if not store_id:
            return await call_next(request)

        from agent.multistore import StoreNotFound, current_store_id, ensure_store_ready

        try:
            await ensure_store_ready(store_id)
        except StoreNotFound:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "STORE_NOT_FOUND",
                    "message": f"Unknown store: {store_id}",
                },
            )

        token = current_store_id.set(store_id)
        try:
            return await call_next(request)
        finally:
            current_store_id.reset(token)


class WebhookBodyLimitMiddleware(BaseHTTPMiddleware):
    """Enforces webhook request body size via the Content-Length header.

    Webhooks come from known providers posting JSON — they always send
    Content-Length. Requests without it (chunked bodies) would bypass a
    length check entirely, so they are rejected outright with 411
    (Length Required). Malformed values get 400; oversized bodies get 413.
    """

    _WEBHOOK_PREFIXES: ClassVar[tuple[str, ...]] = ("/support/webhooks/",)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if any(request.url.path.startswith(p) for p in self._WEBHOOK_PREFIXES):
            content_length = request.headers.get("content-length")
            if content_length is None:
                logger.warning("webhook_missing_content_length", path=request.url.path)
                return JSONResponse(
                    status_code=411,
                    content={
                        "error": "LENGTH_REQUIRED",
                        "message": "Content-Length header required for webhook requests",
                    },
                )
            try:
                length = int(content_length)
            except ValueError:
                logger.warning(
                    "webhook_invalid_content_length",
                    path=request.url.path,
                    content_length=content_length,
                )
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "INVALID_CONTENT_LENGTH",
                        "message": "Content-Length header must be an integer",
                    },
                )
            if length > _MAX_WEBHOOK_BODY_BYTES:
                logger.warning(
                    "webhook_body_too_large",
                    path=request.url.path,
                    content_length=content_length,
                )
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": "PAYLOAD_TOO_LARGE",
                        "message": f"Webhook body exceeds {_MAX_WEBHOOK_BODY_BYTES} byte limit",
                    },
                )
        return await call_next(request)
