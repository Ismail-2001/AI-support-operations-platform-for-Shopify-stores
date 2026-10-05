"""Best-effort operational alerting to a Slack-compatible webhook.

Used for events a human must see: a circuit opened, a webhook payload landed
in the dead-letter queue, the daily cost cap was exceeded. Every event is ALSO
logged as structured log — this module only adds the push notification.

Design rules:
- Never raises: an alerting failure must not break the request being handled.
- Throttled per `dedupe_key` so a dead upstream doesn't produce a notification
  storm (default 300s between alerts with the same key).
- No-op when ALERT_WEBHOOK_URL is unset (the default).
"""

import asyncio
import time

import httpx
import structlog

from agent.config import settings

logger = structlog.get_logger(__name__)

DEFAULT_THROTTLE_SECONDS = 300.0

# dedupe_key -> monotonic time of the last delivered attempt.
_last_sent: dict[str, float] = {}

# Strong references to in-flight fire-and-forget alert tasks (RUF006): dropping
# them risks the GC cancelling a task mid-flight.
_pending_tasks: set[asyncio.Task] = set()


def reset_alert_throttle() -> None:
    """Test helper: forget throttle history."""
    _last_sent.clear()


def _schedule(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


async def send_alert(
    title: str,
    message: str,
    *,
    severity: str = "warning",
    dedupe_key: str | None = None,
    throttle_seconds: float = DEFAULT_THROTTLE_SECONDS,
) -> bool:
    """POST {"text": ...} to ALERT_WEBHOOK_URL. Returns True if a request was sent.

    Silently returns False when unconfigured, throttled, or the POST fails."""
    url = settings.ALERT_WEBHOOK_URL
    if not url:
        return False

    key = dedupe_key or title
    now = time.monotonic()
    last = _last_sent.get(key)
    if last is not None and now - last < throttle_seconds:
        logger.debug("alert_throttled", dedupe_key=key, title=title)
        return False
    # Record before sending so a failing endpoint is also throttled — a dead
    # alert webhook must not turn into a hot retry loop inside a request.
    _last_sent[key] = now

    text = f"[{severity.upper()}] {title}\n{message}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json={"text": text, "title": title, "severity": severity})
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("alert_send_failed", error=str(exc), title=title)
        return False
    logger.info("alert_sent", title=title, severity=severity, dedupe_key=key)
    return True


def alert_on_circuit_open(circuit_name: str) -> None:
    """CircuitBreaker.on_open hook — schedules (does not await) the alert.

    Called from inside async request handling; when no event loop is running
    (plain unit tests) it just logs."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("circuit_opened_no_alert_delivery", circuit=circuit_name)
        return
    _schedule(
        send_alert(
            title=f"Circuit opened: {circuit_name}",
            message=(
                f"The {circuit_name} circuit is OPEN after repeated failures. "
                "Calls now fail fast with 503 until the upstream recovers. "
                "Check /health or /support/health for breaker state."
            ),
            severity="error",
            dedupe_key=f"circuit_open:{circuit_name}",
        )
    )


def attach_breaker_alerts() -> None:
    """Wire every currently-registered breaker to the alert hook. Called at
    registration time by agent.resilience.register_breaker; this function
    exists for modules that registered breakers before alerting was imported."""
    from agent.resilience import _BREAKERS

    for breaker in _BREAKERS.values():
        if breaker.on_open is None:
            breaker.on_open = alert_on_circuit_open
