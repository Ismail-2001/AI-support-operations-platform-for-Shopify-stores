"""Storefront chat widget configuration + publishable key.

Two pieces of state, both in the `app_settings` KV table (no .env churn for
display-only settings):

1. **Widget config** — appearance (color, logo, greeting, enabled). Written by the
   operator console at runtime, read by the public /chat/config endpoint.
2. **Publishable widget key** — identifies this install. It is *publishable* by
   design: it ships inside the <script> tag on the storefront, so it must never be
   treated as a secret that guards money. It only gates opening chat sessions
   (rate-limited per IP); every sensitive capability stays behind the private
   API key on /support/*. WIDGET_KEY in .env overrides the stored key; rotation
   of an env-set key must happen in .env (surfaced as a 409 by the API).
"""

import json
import re
import secrets
from typing import Any

import structlog

from agent.config import settings
from agent.setup_store import get_kv, set_kv

logger = structlog.get_logger(__name__)

CONFIG_KEY = "widget_config"
KEY_NAME = "widget_key"

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

_WIDGET_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "title": "Chat with us",
    "greeting": "Hi! How can we help you today?",
    "welcome_message": (
        "Ask us about orders, returns, or products — a real person can jump in anytime."
    ),
    "color": "#2E8C82",
    "logo_url": "",
    "show_confidence": False,
}

_STRING_FIELDS = {
    "title": 80,
    "greeting": 200,
    "welcome_message": 400,
    "logo_url": 500,
    "color": 7,
}


async def get_widget_config() -> dict[str, Any]:
    """Widget appearance with defaults filled — every field is always present."""
    raw = await get_kv(CONFIG_KEY)
    stored: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                stored = parsed
        except (ValueError, TypeError):
            stored = {}
    config = dict(_WIDGET_DEFAULTS)
    for field in _WIDGET_DEFAULTS:
        if field in stored:
            config[field] = stored[field]
    # Re-validate stored values — a hand-edited row must not bypass the same
    # rules the PUT endpoint enforces.
    return _validate(config)


def _validate(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["enabled"] = bool(data.get("enabled", True))
    out["show_confidence"] = bool(data.get("show_confidence", False))
    for field, max_len in _STRING_FIELDS.items():
        value = data.get(field, _WIDGET_DEFAULTS[field])
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string")
        value = value.strip()
        if len(value) > max_len:
            raise ValueError(f"{field} must be at most {max_len} characters")
        out[field] = value
    if out["color"] and not _HEX_COLOR.match(out["color"]):
        raise ValueError("color must be a hex color like #2E8C82")
    if out["logo_url"] and not out["logo_url"].startswith(("http://", "https://")):
        raise ValueError("logo_url must be an http(s) URL")
    return out


async def set_widget_config(data: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist widget config (partial update over current). Unknown keys dropped."""
    merged = await get_widget_config()
    merged.update({k: v for k, v in data.items() if v is not None})
    config = _validate(merged)
    await set_kv(CONFIG_KEY, json.dumps(config))
    logger.info("widget_config_updated", enabled=config["enabled"])
    return config


async def get_widget_key() -> str:
    """The effective publishable key: .env override, else stored, else generate once."""
    if settings.WIDGET_KEY:
        return settings.WIDGET_KEY.get_secret_value()
    stored = await get_kv(KEY_NAME)
    if stored:
        return stored
    key = secrets.token_urlsafe(24)
    await set_kv(KEY_NAME, key)
    logger.info("widget_key_generated")
    return key


def widget_key_is_env_managed() -> bool:
    return bool(settings.WIDGET_KEY)


async def rotate_widget_key() -> str:
    """Generate a fresh key and persist it. Callers must check env-managed first."""
    key = secrets.token_urlsafe(24)
    await set_kv(KEY_NAME, key)
    logger.info("widget_key_rotated")
    return key
