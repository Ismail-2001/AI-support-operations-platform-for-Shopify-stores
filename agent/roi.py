"""ROI / impact report for the dashboard.

Design principle: LLM cost is *measured*; time saved is an *estimate* driven by
the store owner's own assumptions (how long a human takes to handle a ticket
alone). Both are shown side by side, assumptions are visible and editable, and
the math lives here as pure functions so tests can pin it down.

Credit model (deliberately conservative):
- Auto-sent ticket (zero human touch): full `minutes_per_auto_sent` credit.
- Draft that a human reviewed + approved: partial `minutes_per_draft` credit —
  reviewing is faster than writing from scratch, but it is not free.
- Tickets nobody responded to get no credit.
"""

from typing import Any

from agent.setup_store import get_kv, set_kv

DEFAULT_SETTINGS: dict[str, float] = {
    "minutes_per_auto_sent": 10.0,
    "minutes_per_draft": 5.0,
    "hourly_rate_usd": 25.0,
}

_KV_MAP: dict[str, str] = {
    "minutes_per_auto_sent": "roi_minutes_per_auto_sent",
    "minutes_per_draft": "roi_minutes_per_draft",
    "hourly_rate_usd": "roi_hourly_rate_usd",
}


async def get_roi_settings() -> dict[str, float]:
    """Stored assumptions with defaults for anything unset/unparseable."""
    out: dict[str, float] = {}
    for field, kv_key in _KV_MAP.items():
        raw = await get_kv(kv_key)
        try:
            out[field] = float(raw) if raw is not None else DEFAULT_SETTINGS[field]
        except (TypeError, ValueError):
            out[field] = DEFAULT_SETTINGS[field]
    return out


async def save_roi_settings(updates: dict[str, float]) -> dict[str, float]:
    """Persist assumptions (range-validated by the endpoint's pydantic model)."""
    for field, kv_key in _KV_MAP.items():
        if field in updates:
            await set_kv(kv_key, str(updates[field]))
    return await get_roi_settings()


def compute_roi(raw: dict[str, Any], assumptions: dict[str, float]) -> dict[str, Any]:
    """Raw aggregates + assumptions -> full ROI report. Pure (no I/O).

    `raw` is the dict from storage.get_roi_aggregates; negative net values are
    passed through untouched — if LLM cost exceeds estimated savings we say so."""
    minutes_auto = float(assumptions["minutes_per_auto_sent"])
    minutes_draft = float(assumptions["minutes_per_draft"])
    rate = float(assumptions["hourly_rate_usd"])

    total = int(raw["total"])
    auto_sent = int(raw["auto_sent"])
    responded = int(raw["responded"])
    edited = int(raw["edited"])
    drafts = max(0, responded - auto_sent)
    cost = float(raw["llm_cost_usd"])

    auto_hours = auto_sent * minutes_auto / 60
    draft_hours = drafts * minutes_draft / 60
    hours_saved = auto_hours + draft_hours
    labor_saved = hours_saved * rate
    net = labor_saved - cost
    roi_pct = (net / cost * 100) if cost > 0 else None

    by_day: dict[str, dict[str, int]] = raw.get("by_day", {})
    cost_by_day: dict[str, float] = raw.get("llm_cost_by_day", {})
    series: list[dict[str, Any]] = []
    for date in sorted(set(by_day) | set(cost_by_day)):
        day = by_day.get(date, {})
        day_auto = int(day.get("auto_sent", 0))
        day_responded = int(day.get("responded", 0))
        day_drafts = max(0, day_responded - day_auto)
        day_hours = day_auto * minutes_auto / 60 + day_drafts * minutes_draft / 60
        day_saved = day_hours * rate
        day_cost = cost_by_day.get(date, 0.0)
        series.append(
            {
                "date": date,
                "tickets": int(day.get("tickets", 0)),
                "auto_sent": day_auto,
                "hours_saved": round(day_hours, 3),
                "labor_saved_usd": round(day_saved, 2),
                "llm_cost_usd": round(day_cost, 6),
                "net_usd": round(day_saved - day_cost, 4),
            }
        )

    return {
        "window": {
            "total": total,
            "auto_sent": auto_sent,
            "responded": responded,
            "edited": edited,
            "drafts_reviewed": drafts,
            "llm_cost_usd": cost,
        },
        "assumptions": assumptions,
        "hours_saved": round(hours_saved, 3),
        "labor_saved_usd": round(labor_saved, 2),
        "net_savings_usd": round(net, 2),
        "roi_percent": round(roi_pct, 1) if roi_pct is not None else None,
        "cost_per_ticket_usd": round(cost / total, 6) if total else None,
        "draft_edit_rate": round(edited / responded, 3) if responded else 0.0,
        "by_channel": raw.get("by_channel", {}),
        "category_tickets": raw.get("category_tickets", {}),
        "category_edits": raw.get("category_edits", {}),
        "series": series,
    }
