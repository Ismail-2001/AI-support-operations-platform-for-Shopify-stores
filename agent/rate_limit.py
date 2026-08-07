"""
Simple in-memory sliding-window rate limiter, keyed by client IP.

Deliberately dependency-free — good enough for a single-instance deploy (which is what
render.yaml describes). If you scale to multiple instances behind a load balancer, move
this to Redis (INCR + EXPIRE) so limits are shared across instances instead of per-process.
"""

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from agent.config import settings

# ip -> deque of request timestamps within the current window
_request_log: dict[str, deque[float]] = defaultdict(deque)


def _check_rate_limit(client_ip: str, limit_per_minute: int) -> None:
    now = time.monotonic()
    window_start = now - 60.0
    log = _request_log[client_ip]

    while log and log[0] < window_start:
        log.popleft()

    if len(log) >= limit_per_minute:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({limit_per_minute}/minute). Try again shortly.",
        )
    log.append(now)


def _client_ip(request: Request) -> str:
    """Extract client IP for rate limiting.
    x-forwarded-for is spoofable — only trust it in production behind a known proxy.
    In development, always use the direct connection IP."""
    if settings.ENV == "development":
        # Development: never trust x-forwarded-for (easily spoofed)
        return request.client.host if request.client else "unknown"

    # Production: behind Render's load balancer, x-forwarded-for is set by the proxy.
    # We still prefer the direct IP when available, but fall back to x-forwarded-for
    # for requests that come through the proxy chain.
    if request.client:
        return request.client.host

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()

    return "unknown"


async def rate_limit_default(request: Request) -> None:
    _check_rate_limit(_client_ip(request), settings.RATE_LIMIT_PER_MINUTE)


async def rate_limit_refund(request: Request) -> None:
    _check_rate_limit(_client_ip(request), settings.REFUND_RATE_LIMIT_PER_MINUTE)


async def rate_limit_resend(request: Request) -> None:
    _check_rate_limit(_client_ip(request), settings.RESEND_RATE_LIMIT_PER_MINUTE)
