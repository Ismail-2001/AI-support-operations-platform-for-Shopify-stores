"""Return-eligibility rules for prepaid return labels.

Kept as pure functions so the API endpoint AND the evals can exercise exactly the
same policy: window from the order's created_at, order must actually be fulfilled,
and a fully refunded order has nothing left to return.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from agent.config import settings


def _parse_created_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def evaluate_return_eligibility(
    order: dict[str, Any] | None,
    window_days: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Decide whether a return label may be created for `order`.

    Returns a plain dict the endpoint returns as-is:
    {eligible, reason, window_days, last_return_date, line_items}
    """
    window = int(window_days if window_days is not None else settings.RETURN_WINDOW_DAYS)
    result: dict[str, Any] = {
        "eligible": False,
        "reason": None,
        "window_days": window,
        "last_return_date": None,
        "line_items": [],
    }

    if not order:
        result["reason"] = "No order linked to this ticket"
        return result

    financial = str(order.get("financial_status") or "").lower()
    if financial == "refunded":
        result["reason"] = "Order is fully refunded — nothing left to return"
        return result

    fulfillment = str(order.get("fulfillment_status") or "").lower() or "unfulfilled"
    if fulfillment == "unfulfilled":
        result["reason"] = "Order has not shipped yet — cancel it instead of returning"
        return result

    created = _parse_created_at(order.get("created_at"))
    if created is None:
        # Unknown order date: do NOT silently approve a paid label. Surface it so a
        # human decides — the window is a merchant policy, not something to guess.
        result["reason"] = "Order date unknown — cannot verify the return window"
        return result
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    last_date = created + timedelta(days=window)
    result["last_return_date"] = last_date.date().isoformat()

    current = now or datetime.now(UTC)
    if current > last_date:
        days_over = (current - last_date).days
        result["reason"] = f"Return window expired {days_over} day(s) ago (window: {window} days)"
        return result

    result["eligible"] = True
    result["line_items"] = [
        {
            "line_item_id": li.get("id"),
            "title": li.get("title"),
            "variant_title": li.get("variant_title"),
            "quantity": li.get("quantity"),
            "sku": li.get("sku"),
        }
        for li in order.get("line_items") or []
    ]
    return result
