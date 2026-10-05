"""Runtime setup state for the client-onboarding wizard.

Two storage mechanisms, deliberately different:

1. **Brand voice** — non-secret display text (store name, tone, sign-off) lives in
   the `app_settings` KV table in SQLite so the wizard can write it at runtime
   without touching the filesystem or restarting the process.

2. **Shopify credentials** — secrets belong in `.env` (matches how every other
   integration is configured, survives restarts, never lands in the DB file).
   The wizard validates the credentials against Shopify first, then writes them
   through to `.env` AND mutates the in-memory `settings` singleton so the
   running process picks them up immediately — no restart required.
"""

import re
from pathlib import Path
from typing import Any

import aiosqlite
import structlog
from pydantic import SecretStr

from agent.config import settings

logger = structlog.get_logger(__name__)

VOICE_KEY = "brand_voice"

# Tests redirect this so a setup test never rewrites the real .env.
ENV_PATH: Path | None = None

_VOICE_DEFAULTS: dict[str, str] = {
    "store_name": "",
    "tone": "friendly",
    "sign_off": "",
    "support_email": "",
}

_TONES = {"friendly", "professional", "casual"}

_ENV_KEYS = ("SHOPIFY_SHOP_DOMAIN", "SHOPIFY_ACCESS_TOKEN")


# ── KV helpers ────────────────────────────────────────────────────────────────
#
# KV reads/writes follow the same store-scoping rule as automation thresholds
# (agent/automation.py): the app_settings table lives in whichever TicketStore
# DB the current request resolves to - the per-store file inside an X-Store-Id
# scope, the primary DB otherwise. This is what isolates brand voice, widget
# config, ROI assumptions, and wizard flags per tenant. The store *registry*
# deliberately does NOT use these helpers (it always targets settings.DB_PATH).


def _kv_db_path() -> str:
    import agent.storage as storage_module

    return storage_module.store.db_path


async def get_kv(key: str) -> str | None:
    async with aiosqlite.connect(_kv_db_path()) as db:
        cursor = await db.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
    return row[0] if row else None


async def set_kv(key: str, value: str) -> None:
    async with aiosqlite.connect(_kv_db_path()) as db:
        await db.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


async def get_voice() -> dict[str, str]:
    """Brand voice config with defaults — every field is always present."""
    import json

    raw = await get_kv(VOICE_KEY)
    if not raw:
        return dict(_VOICE_DEFAULTS)
    try:
        stored = json.loads(raw)
    except (ValueError, TypeError):
        return dict(_VOICE_DEFAULTS)
    voice = dict(_VOICE_DEFAULTS)
    if isinstance(stored, dict):
        for field in _VOICE_DEFAULTS:
            value = stored.get(field)
            if isinstance(value, str):
                voice[field] = value
    if voice["tone"] not in _TONES:
        voice["tone"] = "friendly"
    return voice


async def set_voice(data: dict[str, Any]) -> dict[str, str]:
    """Validate and persist brand voice. Unknown keys are dropped."""
    import json

    voice = dict(_VOICE_DEFAULTS)
    for field in _VOICE_DEFAULTS:
        value = data.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string")
        voice[field] = value.strip()
    if voice["tone"] not in _TONES:
        raise ValueError(f"tone must be one of {sorted(_TONES)}")
    await set_kv(VOICE_KEY, json.dumps(voice))
    return voice


# ── Shopify credential write-through ─────────────────────────────────────────


def normalize_shop_domain(raw: str) -> str:
    """Accepts what a human types — 'https://mystore.myshopify.com/admin',
    'mystore.myshopify.com/', 'Mystore' — and returns a bare shop domain."""
    domain = raw.strip().lower()
    domain = re.sub(r"^https?://", "", domain)
    domain = domain.split("/")[0]
    if not domain:
        raise ValueError("shop domain is empty")
    if "." not in domain:
        domain = f"{domain}.myshopify.com"
    if not domain.endswith(".myshopify.com") and domain.count(".") < 1:
        raise ValueError(f"'{raw}' does not look like a shop domain")
    return domain


def persist_shopify_to_env(domain: str, token: str) -> Path:
    """Write/replace SHOPIFY_* keys in .env, preserving every other line
    (comments, other keys, ordering). Returns the path written."""
    env_path = ENV_PATH or Path(".env")
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    updates = {"SHOPIFY_SHOP_DOMAIN": domain, "SHOPIFY_ACCESS_TOKEN": token}
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else None
        if key in updates and not line.strip().startswith("#"):
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")

    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return env_path


def apply_shopify_to_settings(domain: str, token: str) -> None:
    """Mutate the running settings singleton so the current process picks the
    credentials up without a restart. .env write handles the next boot."""
    settings.SHOPIFY_SHOP_DOMAIN = domain
    settings.SHOPIFY_ACCESS_TOKEN = SecretStr(token)
