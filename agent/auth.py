"""
API key authentication. Every /support/* endpoint except the webhook endpoints depends
on verify_api_key. Webhooks (Gorgias, generic inbound) use their own shared-secret checks
instead, since the calling services can't always be configured with a custom auth header.
"""

import hmac

from fastapi import Header, HTTPException

from agent.config import settings


async def verify_api_key(x_api_key: str = Header(default="")) -> None:
    if not settings.REQUIRE_API_KEY:
        return  # explicitly opted out — fine for local dev, never do this in production

    if not settings.API_KEY:
        raise HTTPException(
            status_code=500,
            detail="REQUIRE_API_KEY is true but API_KEY is not set in .env — refusing to run open.",
        )

    expected = settings.API_KEY.get_secret_value()
    # constant-time comparison — a naive `!=` check leaks timing information an attacker
    # can use to guess the key one character at a time over enough requests.
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")


def check_shared_secret(provided: str | None, expected: str | None, name: str) -> None:
    """Used by webhook endpoints (Gorgias, generic inbound) instead of verify_api_key.
    In production (ENV != 'development'), a missing secret is a configuration error
    that should fail loudly — an open webhook endpoint is a security vulnerability."""
    if not expected:
        if settings.ENV == "development":
            # Dev/testing: no secret configured is allowed for local convenience.
            return
        # Production: missing secret is a hard failure — never silently bypass.
        raise HTTPException(
            status_code=500,
            detail=f"{name} webhook secret not configured — refusing to accept unauthenticated webhooks.",
        )
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail=f"Invalid or missing {name} secret")
