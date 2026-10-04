"""Multi-store / agency packaging foundations.

Two layers:

1. **Registry** (primary DB): the ``stores`` table — id, name, shop_domain,
   credentials JSON. Registry access always targets ``settings.DB_PATH``
   directly and *ignores* the request store context, so admin endpoints stay
   global while data-plane requests are isolated.

2. **Data plane** (per-store DB files): tickets, messages, audits and KB
   chunks for a store live in a sibling file ``cs_store_<id>.db`` next to the
   primary DB, opened lazily on the first request carrying ``X-Store-Id``.

Request flow: :class:`StoreContextMiddleware` validates the store id (404
otherwise), awaits :func:`ensure_store_ready` (per-store ``TicketStore`` /
``KnowledgeBase`` / agent built and cached), then sets the
``current_store_id`` ContextVar. Module-level singletons wrapped in
:class:`~agent.context_proxy.ContextProxy` resolve through it.

Default context (no header) resolves to the same objects as before —
single-store deploys and all existing tests are unaffected.

Credentials are write-only: responses expose ``has_shopify_token``, never the
token itself. Deleting a store drops its caches but keeps the per-store data
file on disk (no silent data destruction); re-creating the store reuses it.
"""

import json
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import structlog

from agent.config import settings
from agent.knowledge_base import KnowledgeBase
from agent.storage import TicketStore

logger = structlog.get_logger(__name__)

current_store_id: ContextVar[str | None] = ContextVar("current_store_id", default=None)

_REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    shop_domain TEXT NOT NULL DEFAULT '',
    credentials_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class StoreNotFound(LookupError):
    """Unknown store id on the X-Store-Id header or registry lookup."""

    def __init__(self, store_id: str):
        super().__init__(f"Unknown store: {store_id}")
        self.store_id = store_id


def get_store_id() -> str | None:
    """Store id for the current request context, or None outside a request."""
    return current_store_id.get()


def store_db_path(store_id: str) -> str:
    """Per-store data file, sibling of the primary DB."""
    base = Path(settings.DB_PATH)
    return str(base.with_name(f"cs_store_{store_id}.db"))


def kb_db_path(store_id: str) -> str:
    base = Path(settings.DB_PATH)
    return str(base.with_name(f"kb_store_{store_id}.db"))


@asynccontextmanager
async def _connect():
    # NOTE: `async with aiosqlite.connect(...)` awaits (starts) the connection
    # exactly once — never do `async with await connect()`, that starts the
    # underlying thread twice and raises "threads can only be started once".
    db = await aiosqlite.connect(settings.DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        await db.executescript(_REGISTRY_SCHEMA)
        yield db
    finally:
        await db.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_dict(row: aiosqlite.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "shop_domain": row["shop_domain"],
        "credentials": json.loads(row["credentials_json"] or "{}"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def public_store(rec: dict) -> dict:
    """Registry record with credentials redacted for API responses."""
    return {
        "id": rec["id"],
        "name": rec["name"],
        "shop_domain": rec["shop_domain"],
        "has_shopify_token": bool(rec["credentials"].get("shopify_access_token")),
        "created_at": rec["created_at"],
        "updated_at": rec["updated_at"],
    }


async def list_stores() -> list[dict]:
    async with _connect() as db:
        cursor = await db.execute("SELECT * FROM stores ORDER BY created_at")
        rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


async def get_store(store_id: str) -> dict | None:
    async with _connect() as db:
        cursor = await db.execute("SELECT * FROM stores WHERE id = ?", (store_id,))
        row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


async def create_store(
    name: str,
    shop_domain: str = "",
    credentials: dict | None = None,
) -> dict:
    store_id = uuid.uuid4().hex
    now = _now()
    async with _connect() as db:
        await db.execute(
            "INSERT INTO stores (id, name, shop_domain, credentials_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (store_id, name, shop_domain, json.dumps(credentials or {}), now, now),
        )
        await db.commit()
    rec = await get_store(store_id)
    assert rec is not None
    logger.info("store_created", store_id=store_id, name=name, shop_domain=shop_domain)
    return rec


async def update_store(
    store_id: str,
    *,
    name: str | None = None,
    shop_domain: str | None = None,
    credentials: dict | None = None,
) -> dict | None:
    rec = await get_store(store_id)
    if rec is None:
        return None
    merged = dict(rec["credentials"])
    if credentials is not None:
        for key, value in credentials.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
    async with _connect() as db:
        await db.execute(
            "UPDATE stores SET name = ?, shop_domain = ?, credentials_json = ?, updated_at = ? WHERE id = ?",
            (
                name if name is not None else rec["name"],
                shop_domain if shop_domain is not None else rec["shop_domain"],
                json.dumps(merged),
                _now(),
                store_id,
            ),
        )
        await db.commit()
    invalidate_store(store_id)
    updated = await get_store(store_id)
    assert updated is not None
    return updated


async def delete_store(store_id: str) -> bool:
    async with _connect() as db:
        cursor = await db.execute("DELETE FROM stores WHERE id = ?", (store_id,))
        await db.commit()
        deleted = cursor.rowcount > 0
    if deleted:
        invalidate_store(store_id)
        logger.info("store_deleted", store_id=store_id, data_file_kept=True)
    return deleted


# --- Per-store caches (populated by ensure_store_ready, read sync by proxies) ---

_store_records: dict[str, dict] = {}
_ticket_stores: dict[str, TicketStore] = {}
_kbs: dict[str, KnowledgeBase] = {}
_agents: dict[str, object] = {}


def invalidate_store(store_id: str) -> None:
    _store_records.pop(store_id, None)
    _ticket_stores.pop(store_id, None)
    _kbs.pop(store_id, None)
    _agents.pop(store_id, None)


def reset_caches() -> None:
    """Test helper: drop all per-store caches (does not delete DB files)."""
    for store_id in list(_ticket_stores):
        invalidate_store(store_id)


def _build_agent(rec: dict):
    from agent.support_agent import CustomerSupportAgent
    from integrations.shopify import ShopifyClient

    creds = rec.get("credentials") or {}
    token = creds.get("shopify_access_token")
    shopify = None
    if rec.get("shop_domain"):
        shopify = ShopifyClient(shop_domain=rec["shop_domain"], access_token=token)
    return CustomerSupportAgent(shopify=shopify)


async def ensure_store_ready(store_id: str) -> dict:
    """Validate the store exists and lazily build its data-plane objects.

    Called by the middleware *before* the ContextVar is set, so proxy
    resolvers only ever read the caches synchronously.
    """
    rec = await get_store(store_id)
    if rec is None:
        raise StoreNotFound(store_id)
    if store_id not in _ticket_stores:
        ts = TicketStore(db_path=store_db_path(store_id))
        await ts.init()
        kb = KnowledgeBase(db_path=kb_db_path(store_id))
        await kb.init()
        _ticket_stores[store_id] = ts
        _kbs[store_id] = kb
        _agents[store_id] = _build_agent(rec)
        logger.info("store_context_ready", store_id=store_id, name=rec["name"])
    _store_records[store_id] = rec
    return rec


def _require_cached(store_id: str, cache: dict, kind: str):
    try:
        return cache[store_id]
    except KeyError:
        raise RuntimeError(
            f"{kind} for store {store_id!r} not initialized — ensure_store_ready "
            "must run before the X-Store-Id ContextVar is set"
        ) from None


def ticket_store_for_current() -> TicketStore | None:
    store_id = current_store_id.get()
    if not store_id:
        return None
    return _require_cached(store_id, _ticket_stores, "TicketStore")


def knowledge_base_for_current() -> KnowledgeBase | None:
    store_id = current_store_id.get()
    if not store_id:
        return None
    return _require_cached(store_id, _kbs, "KnowledgeBase")


def agent_for_current():
    store_id = current_store_id.get()
    if not store_id:
        return None
    return _require_cached(store_id, _agents, "CustomerSupportAgent")
