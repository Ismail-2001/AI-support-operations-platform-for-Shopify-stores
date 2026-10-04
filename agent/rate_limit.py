"""
Simple in-memory sliding-window rate limiter, keyed by client IP.

Deliberately dependency-free — good enough for a single-instance deploy (which is what
render.yaml describes). If you scale to multiple instances behind a load balancer, move
this to Redis (INCR + EXPIRE) so limits are shared across instances instead of per-process.
"""

import ipaddress
import time
from collections import defaultdict, deque

from fastapi import Request

from agent.config import settings
from api.errors import raise_rate_limited

# ip -> deque of request timestamps within the current window
_request_log: dict[str, deque[float]] = defaultdict(deque)


def _check_rate_limit(client_ip: str, limit_per_minute: int) -> None:
    now = time.monotonic()
    window_start = now - 60.0
    log = _request_log[client_ip]

    while log and log[0] < window_start:
        log.popleft()

    if len(log) >= limit_per_minute:
        raise_rate_limited(limit_per_minute)
    log.append(now)


def _is_public_ip(value: str) -> bool:
    """Parseable and not private/loopback/link-local (i.e. a real client address)."""
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local)


def _client_ip(request: Request) -> str:
    """Extract client IP for rate limiting.

    Local/development: always use the direct connection IP — x-forwarded-for is
    client-supplied and spoofable, so it is never trusted locally.

    Production: request.client.host is the trusted proxy's IP for *every*
    caller, which would put all traffic into one shared bucket. Walk
    x-forwarded-for from the right (nearest hop first), skip private proxy
    hops, and return the first public IP — the real client. Falls back to
    request.client.host only when the header is missing."""
    if settings.ENV == "development":
        return request.client.host if request.client else "unknown"

    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return request.client.host if request.client else "unknown"

    entries = [e.strip() for e in forwarded.split(",") if e.strip()]
    for entry in reversed(entries):
        if _is_public_ip(entry):
            return entry
    # Header present but only private/unparseable hops — best effort: right-most entry.
    return entries[-1] if entries else (request.client.host if request.client else "unknown")


async def rate_limit_default(request: Request) -> None:
    _check_rate_limit(_client_ip(request), settings.RATE_LIMIT_PER_MINUTE)


async def rate_limit_refund(request: Request) -> None:
    _check_rate_limit(_client_ip(request), settings.REFUND_RATE_LIMIT_PER_MINUTE)


async def rate_limit_resend(request: Request) -> None:
    _check_rate_limit(_client_ip(request), settings.RESEND_RATE_LIMIT_PER_MINUTE)


async def rate_limit_action(request: Request) -> None:
    _check_rate_limit(_client_ip(request), settings.ACTION_RATE_LIMIT_PER_MINUTE)


async def rate_limit_chat(request: Request) -> None:
    """Public storefront chat — per-IP, its own budget (publishable-key auth)."""
    _check_rate_limit(_client_ip(request), settings.CHAT_RATE_LIMIT_PER_MINUTE)
