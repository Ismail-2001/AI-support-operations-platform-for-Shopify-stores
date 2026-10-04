"""Per-category auto-send thresholds + calibration recommendations.

Threshold resolution order (first hit wins):
1. Runtime override in the `app_settings` KV table — written by
   PUT /support/automation/thresholds, takes effect immediately, no restart.
2. The category-specific Settings field (AUTO_SEND_MIN_CONFIDENCE_<CATEGORY>
   in .env — per-tenant config, no code change).
3. AUTO_SEND_MIN_CONFIDENCE_DEFAULT.

The daily cost cap (observability.check_daily_cost_cap) remains an independent
global breaker — raising a category threshold never disables it, and lowering
one can never auto-send past it.

Recommendations are computed from `edit_records` (real human edit behavior on
AI drafts) and only fire with 50+ reviewed samples per category — below that we
explicitly report "not enough data" instead of guessing.
"""

from typing import Any

import structlog

from agent.config import settings

logger = structlog.get_logger(__name__)


async def _kv_get(key: str) -> str | None:
    """Threshold overrides live in the same app_settings KV table as brand voice, but are
    read through the storage singleton (not setup_store's settings.DB_PATH) so tests that
    swap storage_module.store hit their temp database too."""
    import aiosqlite

    import agent.storage as storage_module

    try:
        async with aiosqlite.connect(storage_module.store.db_path) as db:
            cursor = await db.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
            row = await cursor.fetchone()
    except aiosqlite.OperationalError:
        # Table not created yet (store.init() never ran) — no overrides exist.
        return None
    return row[0] if row else None


async def _kv_set(key: str, value: str) -> None:
    import aiosqlite

    import agent.storage as storage_module

    async with aiosqlite.connect(storage_module.store.db_path) as db:
        await db.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


async def _kv_delete(key: str) -> None:
    import aiosqlite

    import agent.storage as storage_module

    async with aiosqlite.connect(storage_module.store.db_path) as db:
        await db.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        await db.commit()


# TicketCategory value -> Settings field holding its confidence floor.
CATEGORY_THRESHOLD_FIELDS: dict[str, str] = {
    "order_status": "AUTO_SEND_MIN_CONFIDENCE_ORDER_STATUS",
    "shipping": "AUTO_SEND_MIN_CONFIDENCE_SHIPPING",
    "product_question": "AUTO_SEND_MIN_CONFIDENCE_PRODUCT_QUESTION",
    "returns": "AUTO_SEND_MIN_CONFIDENCE_RETURNS",
    "technical": "AUTO_SEND_MIN_CONFIDENCE_TECHNICAL",
}

# Categories that may auto-send at all (everything else is hard-blocked in
# graph.decide_auto_send via AUTO_SEND_BLOCKED_CATEGORIES).
AUTO_SENDABLE_CATEGORIES = (
    "order_status",
    "shipping",
    "returns",
    "product_question",
    "technical",
)

MIN_SAMPLES_FOR_RECOMMENDATION = 50
# Hard floor: no category can ever auto-send below this, even via override.
ABSOLUTE_MIN_THRESHOLD = 0.80
# Hard ceiling for recommendations: blocked categories are reported as "never".
BLOCKED_EFFECTIVE_THRESHOLD = None


def _threshold_setting(category: str) -> float:
    field = CATEGORY_THRESHOLD_FIELDS.get(category)
    if field:
        return float(getattr(settings, field))
    return float(settings.AUTO_SEND_MIN_CONFIDENCE_DEFAULT)


def threshold_kv_key(category: str) -> str:
    return f"auto_send_min_confidence:{category}"


async def get_min_confidence(category: str) -> float:
    """Effective confidence floor for auto-sending a ticket of this category."""
    raw = await _kv_get(threshold_kv_key(category))
    if raw is not None:
        try:
            return max(float(raw), ABSOLUTE_MIN_THRESHOLD)
        except ValueError:
            logger.warning("invalid_threshold_override", category=category, raw=raw)
    return _threshold_setting(category)


async def set_min_confidence(category: str, value: float) -> None:
    if category not in AUTO_SENDABLE_CATEGORIES:
        raise ValueError(
            f"'{category}' is not auto-sendable — thresholds only apply to: "
            f"{', '.join(AUTO_SENDABLE_CATEGORIES)}"
        )
    if not (ABSOLUTE_MIN_THRESHOLD <= value <= 1.0):
        raise ValueError(
            f"threshold must be between {ABSOLUTE_MIN_THRESHOLD} and 1.0 (got {value})"
        )
    await _kv_set(threshold_kv_key(category), str(value))
    logger.info("threshold_override_set", category=category, min_confidence=value)


async def get_thresholds() -> list[dict[str, Any]]:
    """Current + configured + default threshold for every auto-sendable category."""
    report = []
    for category in AUTO_SENDABLE_CATEGORIES:
        override_raw = await _kv_get(threshold_kv_key(category))
        override: float | None = None
        if override_raw:
            try:
                override = float(override_raw)
            except ValueError:
                override = None
        report.append(
            {
                "category": category,
                "min_confidence": override
                if override is not None
                else _threshold_setting(category),
                "source": "runtime_override" if override is not None else "settings",
                "env_default": _threshold_setting(category),
            }
        )
    return report


def _bucket_edit_rate(
    samples: list[tuple[float, bool]], lo: float, hi: float
) -> tuple[int, float | None]:
    in_bucket = [edited for conf, edited in samples if lo <= conf < hi]
    if not in_bucket:
        return 0, None
    return len(in_bucket), sum(in_bucket) / len(in_bucket)


_BUCKETS = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.85), (0.85, 0.9), (0.9, 1.01)]


def suggest_threshold(current: float, samples: list[tuple[float, bool]]) -> tuple[float, str]:
    """Given the current floor and (confidence, was_edited) samples for one category,
    return (suggested_threshold, reason). Pure function — unit-tested directly.

    Rules (deliberately conservative, favoring more human review over fewer edits):
    - < 50 samples: keep current, say so.
    - If drafts AT/ABOVE the current floor still get edited >10% of the time in any
      bucket with >= 10 samples: raise to just above that bucket.
    - If the bucket immediately below the current floor has >= 15 samples with a 0%
      edit rate: lower to that bucket's floor (never below ABSOLUTE_MIN_THRESHOLD).
    - Otherwise: keep current.
    """
    if len(samples) < MIN_SAMPLES_FOR_RECOMMENDATION:
        return current, (
            f"Not enough data — {len(samples)}/{MIN_SAMPLES_FOR_RECOMMENDATION} reviewed "
            f"samples for this category"
        )

    for lo, hi in _BUCKETS:
        if lo < current:
            continue
        count, rate = _bucket_edit_rate(samples, lo, hi)
        if count >= 10 and rate is not None and rate > 0.10:
            return min(hi, 1.0), (
                f"Raise: {int(rate * 100)}% of drafts with confidence {lo:.2f}-{min(hi, 1.0):.2f} "
                f"were still edited by a human"
            )

    below = [b for b in _BUCKETS if b[1] <= current]
    if below:
        lo, hi = below[-1]
        count, rate = _bucket_edit_rate(samples, lo, hi)
        if count >= 15 and rate == 0.0:
            lowered = max(lo, ABSOLUTE_MIN_THRESHOLD)
            if lowered < current:
                return lowered, (
                    f"Lower: all {count} reviewed drafts just below {current:.2f} were sent "
                    f"without edits"
                )

    return current, "Keep: edit behavior around the current threshold looks healthy"


async def get_auto_send_report() -> dict[str, Any]:
    """Per-category recommendation report for GET /support/analytics/auto-send."""
    import agent.storage as storage_module

    stats = await storage_module.store.get_category_edit_stats()

    categories: dict[str, Any] = {}
    for category in AUTO_SENDABLE_CATEGORIES:
        entry = stats.get(category)
        samples = entry["samples"] if entry else []
        current = await get_min_confidence(category)
        suggested, reason = suggest_threshold(current, samples)
        categories[category] = {
            "current_threshold": current,
            "suggested_threshold": suggested,
            "recommendation": reason,
            "reviewed_samples": entry["count"] if entry else 0,
            "edited_samples": entry["edited"] if entry else 0,
            "edit_rate": entry["edit_rate"] if entry else None,
        }

    blocked = {c.strip() for c in settings.AUTO_SEND_BLOCKED_CATEGORIES.split(",") if c.strip()}
    return {
        "auto_send_enabled": settings.AUTO_SEND_ENABLED,
        "categories": categories,
        "blocked_categories": sorted(blocked),
        "daily_cost_cap_usd": settings.DAILY_COST_CAP_USD,
        "min_samples_for_recommendation": MIN_SAMPLES_FOR_RECOMMENDATION,
        "absolute_min_threshold": ABSOLUTE_MIN_THRESHOLD,
    }
