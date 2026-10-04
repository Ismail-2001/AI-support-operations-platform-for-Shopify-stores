"""SQLite-backed ticket store. Zero-config, file-based — fine for a single-instance deploy.

Swap for Supabase/Postgres once you're running multiple workers or need concurrent writers
(Render's free tier disk is ephemeral across deploys, so treat this as durable-enough for an
MVP, not as your permanent system of record).
"""

import builtins
import json
import os
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

import aiosqlite
import structlog

from agent.config import settings
from agent.models import ResponseSuggestion, SupportTicket, TicketMessage

logger = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    suggestion TEXT,
    auto_sent INTEGER DEFAULT 0,
    gorgias_ticket_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tickets_gorgias_id ON tickets(gorgias_ticket_id);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    sender_type TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_ticket_id ON messages(ticket_id);

CREATE TABLE IF NOT EXISTS edit_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    ai_suggestion TEXT NOT NULL,
    final_response TEXT NOT NULL,
    was_edited INTEGER NOT NULL,
    similarity REAL NOT NULL,
    category TEXT,
    confidence REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refund_audit (
    idempotency_key TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    amount REAL NOT NULL,
    reason TEXT,
    status TEXT NOT NULL,           -- 'succeeded' or 'failed'
    shopify_response TEXT,
    error TEXT,
    detail TEXT,                    -- JSON: e.g. refunded line items for partial refunds
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resend_audit (
    idempotency_key TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    status TEXT NOT NULL,           -- 'succeeded' or 'failed'
    shopify_response TEXT,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_audit (
    idempotency_key TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    action TEXT NOT NULL,           -- 'cancel_order' or 'edit_address'
    request_json TEXT NOT NULL,     -- what the human approved (reason, address, notify flags)
    status TEXT NOT NULL,           -- 'succeeded' or 'failed'
    shopify_response TEXT,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS return_label_audit (
    idempotency_key TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    status TEXT NOT NULL,           -- 'succeeded' or 'failed'
    provider TEXT,                  -- 'shipengine'
    label_id TEXT,
    label_url TEXT,
    tracking_number TEXT,
    carrier_service TEXT,
    cost_usd REAL,
    request_json TEXT,              -- what the human approved (line items, rma)
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_webhook_events (
    event_id TEXT NOT NULL,
    source TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (event_id, source)
);

CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    stage TEXT NOT NULL,             -- 'classification' | 'response_generation'
    model TEXT,
    prompt_version TEXT,             -- e.g. 'classifier_v1', 'response_v1'
    input_summary TEXT NOT NULL,     -- JSON: what went INTO the prompt (transcript, context)
    output_summary TEXT NOT NULL,    -- JSON: what the LLM returned (structured result)
    latency_ms REAL,
    tokens_input INTEGER,
    tokens_output INTEGER,
    cost_usd REAL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_traces_ticket_id ON traces(ticket_id);

CREATE TABLE IF NOT EXISTS llm_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,              -- YYYY-MM-DD, for fast daily aggregation
    ticket_id TEXT,
    stage TEXT,
    model TEXT,
    tokens_input INTEGER,
    tokens_output INTEGER,
    cost_usd REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_costs_date ON llm_costs(date);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    ticket_id TEXT,                   -- set once the first message creates the ticket
    data TEXT NOT NULL,               -- JSON: email, name, order_number, handoff state
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _ticket_filters(
    status: str | None, category: str | None, priority: str | None
) -> tuple[str, list[Any]]:
    """Build a WHERE clause for ticket filters stored inside the `data` JSON blob.

    Column names come from a fixed tuple — never from user input — so string
    interpolation here is safe; only values are bound as parameters."""
    clauses: list[str] = []
    params: list[Any] = []
    for col, val in (("status", status), ("category", category), ("priority", priority)):
        if val is not None:
            clauses.append(f"json_extract(data, '$.{col}') = ?")
            params.append(val)
    return " AND ".join(clauses), params


# Locations known to be wiped on redeploy/restart:
#  - /opt/render — Render web services without an attached disk (free plan)
#  - /tmp, /var/tmp — temp directories
_EPHEMERAL_DB_PREFIXES = ("/opt/render/", "/tmp/", "/var/tmp/")


def storage_is_ephemeral(db_path: str | None = None) -> bool:
    """True when the DB file sits somewhere known to be wiped on redeploy.

    Flags Render working-directory paths (no attached disk) and temp dirs so
    /health can report `storage: ephemeral`. Local dev, attached disks
    (/var/data/...), and named Docker volumes are NOT flagged. A Docker
    container *without* a volume still loses data on recreate — that is not
    detectable from inside the process, so use `docker compose` (which
    declares volumes) or an attached disk in production."""
    raw = db_path or settings.DB_PATH
    # Check both the raw path (POSIX-style values like /opt/render/... are
    # compared verbatim, so behaviour is identical on Windows dev machines)
    # and the resolved absolute path (catches relative DB_PATH on Render).
    candidates = {raw.replace("\\", "/")}
    with suppress(OSError, ValueError):
        candidates.add(os.path.abspath(raw).replace("\\", "/"))
    return any(c.startswith(_EPHEMERAL_DB_PREFIXES) for c in candidates)


class TicketStore:
    # Bump this when you add a migration. Each migration runs in order only once.
    SCHEMA_VERSION = 4

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or settings.DB_PATH
        # Create the parent directory up front so DB_PATH pointing at a fresh
        # attached-disk mount (e.g. /var/data/cs_agent.db) works on first boot.
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executescript(_SCHEMA)
            await self._ensure_schema_version_table(db)
            await self._run_migrations(db)
            await db.commit()
        logger.info("ticket_store_ready", db_path=self.db_path, schema_version=self.SCHEMA_VERSION)

    async def _ensure_schema_version_table(self, db):
        """Create the schema_version table if it doesn't exist."""
        await db.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )

    async def _get_applied_version(self, db) -> int:
        """Return the highest applied schema version, or 0 if none."""
        cursor = await db.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        return row[0] if row and row[0] else 0

    async def _run_migrations(self, db):
        """Run all pending migrations in order. Each migration is idempotent."""
        applied = await self._get_applied_version(db)
        if applied >= self.SCHEMA_VERSION:
            return

        migrations = {
            1: self._migrate_v1,
            2: self._migrate_v2,
            3: self._migrate_v3,
            4: self._migrate_v4,
        }

        for version in sorted(migrations.keys()):
            if version > applied:
                logger.info("schema_migration_start", from_version=applied, to_version=version)
                await migrations[version](db)
                await db.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (version, datetime.now(UTC).isoformat()),
                )
                logger.info("schema_migration_done", version=version)

    async def _migrate_v1(self, db):
        """v1: Add prompt_version to traces, gorgias_ticket_id to tickets."""
        cursor = await db.execute("PRAGMA table_info(traces)")
        columns = [row[1] for row in await cursor.fetchall()]
        if "prompt_version" not in columns:
            await db.execute("ALTER TABLE traces ADD COLUMN prompt_version TEXT")

        cursor = await db.execute("PRAGMA table_info(tickets)")
        ticket_cols = [row[1] for row in await cursor.fetchall()]
        if "gorgias_ticket_id" not in ticket_cols:
            await db.execute("ALTER TABLE tickets ADD COLUMN gorgias_ticket_id TEXT")
            await db.execute(
                "UPDATE tickets SET gorgias_ticket_id = json_extract(data, '$.gorgias_ticket_id')"
                " WHERE gorgias_ticket_id IS NULL"
            )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tickets_gorgias_id ON tickets(gorgias_ticket_id)"
        )

    async def _migrate_v2(self, db):
        """v2: No-op placeholder. Add future migrations here."""
        pass

    async def _migrate_v3(self, db):
        """v3: refund_audit.detail — JSON blob for partial-refund line-item detail.
        (action_audit is created by the _SCHEMA script on every boot, no ALTER needed.)"""
        cursor = await db.execute("PRAGMA table_info(refund_audit)")
        columns = [row[1] for row in await cursor.fetchall()]
        if "detail" not in columns:
            await db.execute("ALTER TABLE refund_audit ADD COLUMN detail TEXT")

    async def _migrate_v4(self, db):
        """v4: return_label_audit — idempotent audit trail for generated return labels.
        (Created by the _SCHEMA script on every boot; kept as an explicit version so
        pre-existing DBs record that they passed this point.)"""
        await db.execute(
            """CREATE TABLE IF NOT EXISTS return_label_audit (
                idempotency_key TEXT PRIMARY KEY,
                ticket_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                status TEXT NOT NULL,
                provider TEXT,
                label_id TEXT,
                label_url TEXT,
                tracking_number TEXT,
                carrier_service TEXT,
                cost_usd REAL,
                request_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            )"""
        )

    async def save(
        self,
        ticket: SupportTicket,
        suggestion: ResponseSuggestion | None = None,
        auto_sent: bool = False,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        gorgias_id = ticket.gorgias_ticket_id or (
            ticket.metadata.get("gorgias_ticket_id") if isinstance(ticket.metadata, dict) else None
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO tickets (id, data, suggestion, auto_sent, gorgias_ticket_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     data=excluded.data, suggestion=excluded.suggestion,
                     auto_sent=excluded.auto_sent, gorgias_ticket_id=excluded.gorgias_ticket_id,
                     updated_at=excluded.updated_at""",
                (
                    ticket.id,
                    ticket.model_dump_json(),
                    suggestion.model_dump_json() if suggestion else None,
                    int(auto_sent),
                    gorgias_id,
                    ticket.created_at,
                    now,
                ),
            )
            await db.commit()

    async def get(self, ticket_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
            row = await cursor.fetchone()
            return self._row_to_dict(row) if row else None

    async def list(
        self,
        status: str | None = None,
        category: str | None = None,
        priority: str | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Paginated ticket listing with optional filters.

        Filters run in SQL (not Python) so LIMIT/OFFSET paginate the *filtered*
        set — filtering after pagination returned the wrong rows for page > 1."""
        where_sql, params = _ticket_filters(status, category, priority)
        sql = "SELECT * FROM tickets"
        if where_sql:
            sql += f" WHERE {where_sql}"
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, (*params, limit, (page - 1) * limit))
            rows = await cursor.fetchall()
            return [self._row_to_dict(r) for r in rows]

    async def count(
        self,
        status: str | None = None,
        category: str | None = None,
        priority: str | None = None,
    ) -> int:
        """Total tickets matching the filters — for accurate pagination metadata."""
        where_sql, params = _ticket_filters(status, category, priority)
        sql = "SELECT COUNT(*) AS total FROM tickets"
        if where_sql:
            sql += f" WHERE {where_sql}"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, params)
            return (await cursor.fetchone())["total"]

    async def all(self) -> builtins.list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tickets")
            rows = await cursor.fetchall()
            return [self._row_to_dict(r) for r in rows]

    async def analytics(self) -> dict[str, Any]:
        """Aggregate ticket stats via SQL — avoids loading all rows into memory."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT COUNT(*) as total FROM tickets")
            total = (await cursor.fetchone())["total"]

            cursor = await db.execute("SELECT COUNT(*) as cnt FROM tickets WHERE auto_sent = 1")
            auto_sent = (await cursor.fetchone())["cnt"]

            cursor = await db.execute(
                """SELECT
                    json_extract(data, '$.status') as val, COUNT(*) as cnt
                   FROM tickets WHERE json_extract(data, '$.status') IN ('open', 'in_progress')
                   OR json_extract(data, '$.status') IS NULL GROUP BY val"""
            )
            open_count = sum(r["cnt"] for r in await cursor.fetchall())

            _ALLOWED_BREAKDOWN_COLS = {"category", "priority", "channel", "sentiment"}

            async def _breakdown(col: str) -> dict[str, int]:
                if col not in _ALLOWED_BREAKDOWN_COLS:
                    raise ValueError(f"Invalid breakdown column: {col}")
                cursor = await db.execute(
                    f"SELECT json_extract(data, '$.{col}') as val, COUNT(*) as cnt"
                    f" FROM tickets WHERE json_extract(data, '$.{col}') IS NOT NULL"
                    f" AND json_extract(data, '$.{col}') != '' GROUP BY val"
                )
                return {r["val"]: r["cnt"] for r in await cursor.fetchall()}

            return {
                "total": total,
                "open": open_count,
                "auto_sent": auto_sent,
                "category_breakdown": await _breakdown("category"),
                "priority_breakdown": await _breakdown("priority"),
                "channel_breakdown": await _breakdown("channel"),
                "sentiment_distribution": await _breakdown("sentiment"),
            }

    async def update_status(self, ticket_id: str, **updates) -> dict[str, Any] | None:
        existing = await self.get(ticket_id)
        if not existing:
            return None
        ticket_data = existing["ticket"]
        ticket_data.update({k: v for k, v in updates.items() if v is not None})
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE tickets SET data = ?, updated_at = ? WHERE id = ?",
                (json.dumps(ticket_data), now, ticket_id),
            )
            await db.commit()
        return await self.get(ticket_id)

    async def get_ticket_model(self, ticket_id: str) -> SupportTicket | None:
        """Rehydrate a full SupportTicket object from storage (for follow-up processing)."""
        row = await self.get(ticket_id)
        if not row:
            return None
        return SupportTicket(**row["ticket"])

    async def get_ticket_by_gorgias_id(self, gorgias_ticket_id: str) -> SupportTicket | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT data FROM tickets WHERE gorgias_ticket_id = ? LIMIT 1",
                (str(gorgias_ticket_id),),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return SupportTicket(**json.loads(row["data"]))

    async def add_message(self, ticket_id: str, sender_type: str, content: str) -> TicketMessage:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO messages (ticket_id, sender_type, content, created_at) VALUES (?, ?, ?, ?)",
                (ticket_id, sender_type, content, now),
            )
            await db.commit()
            message_id = cursor.lastrowid
        return TicketMessage(
            id=message_id,
            ticket_id=ticket_id,
            sender_type=sender_type,
            content=content,
            created_at=now,
        )

    async def get_messages(self, ticket_id: str) -> builtins.list[TicketMessage]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM messages WHERE ticket_id = ? ORDER BY id ASC", (ticket_id,)
            )
            rows = await cursor.fetchall()
            return [
                TicketMessage(
                    id=r["id"],
                    ticket_id=r["ticket_id"],
                    sender_type=r["sender_type"],
                    content=r["content"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    async def log_edit(
        self,
        ticket_id: str,
        ai_suggestion: str,
        final_response: str,
        category: str | None = None,
        confidence: float | None = None,
    ) -> None:
        """Called every time a human sends a reply that started from an AI draft. Tracking how
        much humans change the AI's drafts, broken down by category, is the honest version of
        'self-improvement' for a system like this: it doesn't retrain itself, but it tells you
        exactly which categories need prompt work or more knowledge base content next."""
        similarity = _text_similarity(ai_suggestion, final_response)
        was_edited = similarity < 0.98
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO edit_records (ticket_id, ai_suggestion, final_response, was_edited, "
                "similarity, category, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ticket_id,
                    ai_suggestion,
                    final_response,
                    int(was_edited),
                    similarity,
                    category,
                    confidence,
                    now,
                ),
            )
            await db.commit()

    async def get_edit_stats(self) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM edit_records")
            rows = await cursor.fetchall()

        total = len(rows)
        edited = sum(1 for r in rows if r["was_edited"])
        by_category: dict[str, dict[str, Any]] = {}
        for r in rows:
            cat = r["category"] or "unknown"
            bucket = by_category.setdefault(cat, {"total": 0, "edited": 0, "avg_similarity": 0.0})
            bucket["total"] += 1
            bucket["edited"] += int(r["was_edited"])

        for cat, bucket in by_category.items():
            cat_rows = [r for r in rows if (r["category"] or "unknown") == cat]
            bucket["avg_similarity"] = round(
                sum(r["similarity"] for r in cat_rows) / len(cat_rows), 3
            )
            bucket["edit_rate"] = (
                round(bucket["edited"] / bucket["total"], 3) if bucket["total"] else 0.0
            )

        return {
            "total_ai_drafts_sent": total,
            "edited_before_send": edited,
            "overall_edit_rate": round(edited / total, 3) if total else None,
            "by_category": by_category,
        }

    async def get_refund_audit(self, idempotency_key: str) -> dict[str, Any] | None:
        """If this idempotency key was already processed, return the stored result instead
        of letting the caller re-execute a real refund."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM refund_audit WHERE idempotency_key = ?", (idempotency_key,)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "idempotency_key": row["idempotency_key"],
                "ticket_id": row["ticket_id"],
                "order_id": row["order_id"],
                "amount": row["amount"],
                "reason": row["reason"],
                "status": row["status"],
                "shopify_response": json.loads(row["shopify_response"])
                if row["shopify_response"]
                else None,
                "error": row["error"],
                "detail": json.loads(row["detail"]) if row["detail"] else None,
                "created_at": row["created_at"],
            }

    async def record_refund_audit(
        self,
        idempotency_key: str,
        ticket_id: str,
        order_id: str,
        amount: float,
        reason: str,
        status: str,
        shopify_response: dict[str, Any] | None = None,
        error: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO refund_audit (idempotency_key, ticket_id, order_id, amount, reason, "
                "status, shopify_response, error, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    idempotency_key,
                    ticket_id,
                    order_id,
                    amount,
                    reason,
                    status,
                    json.dumps(shopify_response) if shopify_response else None,
                    error,
                    json.dumps(detail) if detail else None,
                    now,
                ),
            )
            await db.commit()

    async def get_resend_audit(self, idempotency_key: str) -> dict[str, Any] | None:
        """If this idempotency key was already processed, return the stored result instead
        of letting the caller re-execute a real reorder."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM resend_audit WHERE idempotency_key = ?", (idempotency_key,)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "idempotency_key": row["idempotency_key"],
                "ticket_id": row["ticket_id"],
                "order_id": row["order_id"],
                "status": row["status"],
                "shopify_response": json.loads(row["shopify_response"])
                if row["shopify_response"]
                else None,
                "error": row["error"],
                "created_at": row["created_at"],
            }

    async def record_resend_audit(
        self,
        idempotency_key: str,
        ticket_id: str,
        order_id: str,
        status: str,
        shopify_response: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO resend_audit (idempotency_key, ticket_id, order_id, status, "
                "shopify_response, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    idempotency_key,
                    ticket_id,
                    order_id,
                    status,
                    json.dumps(shopify_response) if shopify_response else None,
                    error,
                    now,
                ),
            )
            await db.commit()

    async def get_action_audit(self, idempotency_key: str) -> dict[str, Any] | None:
        """If this idempotency key was already processed, return the stored result instead
        of letting the caller re-execute a real cancel / address edit."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM action_audit WHERE idempotency_key = ?", (idempotency_key,)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "idempotency_key": row["idempotency_key"],
                "ticket_id": row["ticket_id"],
                "order_id": row["order_id"],
                "action": row["action"],
                "request": json.loads(row["request_json"]) if row["request_json"] else None,
                "status": row["status"],
                "shopify_response": json.loads(row["shopify_response"])
                if row["shopify_response"]
                else None,
                "error": row["error"],
                "created_at": row["created_at"],
            }

    async def record_action_audit(
        self,
        idempotency_key: str,
        ticket_id: str,
        order_id: str,
        action: str,
        request: dict[str, Any],
        status: str,
        shopify_response: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO action_audit (idempotency_key, ticket_id, order_id, action, "
                "request_json, status, shopify_response, error, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    idempotency_key,
                    ticket_id,
                    order_id,
                    action,
                    json.dumps(request),
                    status,
                    json.dumps(shopify_response) if shopify_response else None,
                    error,
                    now,
                ),
            )
            await db.commit()

    async def get_return_label_audit(self, idempotency_key: str) -> dict[str, Any] | None:
        """If this idempotency key was already processed, return the stored label result
        instead of buying a second (paid) return label."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM return_label_audit WHERE idempotency_key = ?", (idempotency_key,)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "idempotency_key": row["idempotency_key"],
                "ticket_id": row["ticket_id"],
                "order_id": row["order_id"],
                "status": row["status"],
                "provider": row["provider"],
                "label_id": row["label_id"],
                "label_url": row["label_url"],
                "tracking_number": row["tracking_number"],
                "carrier_service": row["carrier_service"],
                "cost_usd": row["cost_usd"],
                "request": json.loads(row["request_json"]) if row["request_json"] else None,
                "error": row["error"],
                "created_at": row["created_at"],
            }

    async def record_return_label_audit(
        self,
        idempotency_key: str,
        ticket_id: str,
        order_id: str,
        status: str,
        provider: str | None = None,
        label_id: str | None = None,
        label_url: str | None = None,
        tracking_number: str | None = None,
        carrier_service: str | None = None,
        cost_usd: float | None = None,
        request: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO return_label_audit (idempotency_key, ticket_id, order_id, status, "
                "provider, label_id, label_url, tracking_number, carrier_service, cost_usd, "
                "request_json, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    idempotency_key,
                    ticket_id,
                    order_id,
                    status,
                    provider,
                    label_id,
                    label_url,
                    tracking_number,
                    carrier_service,
                    cost_usd,
                    json.dumps(request) if request else None,
                    error,
                    now,
                ),
            )
            await db.commit()

    async def get_processed_webhook_event(
        self, event_id: str, source: str
    ) -> dict[str, Any] | None:
        """If this webhook event was already processed, return it so the caller can skip
        re-processing instead of duplicating work."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM processed_webhook_events WHERE event_id = ? AND source = ?",
                (event_id, source),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "event_id": row["event_id"],
                "source": row["source"],
                "processed_at": row["processed_at"],
            }

    async def record_processed_webhook_event(self, event_id: str, source: str) -> None:
        """Mark a webhook event as successfully processed so redeliveries are ignored."""
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO processed_webhook_events (event_id, source, processed_at) VALUES (?, ?, ?)",
                (event_id, source, now),
            )
            await db.commit()

    async def get_category_edit_stats(self) -> dict[str, Any]:
        """Per-category edit stats for auto-send calibration recommendations:
        {category: {count, edited, edit_rate, samples: [(confidence, was_edited)]}}."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT category, confidence, was_edited FROM edit_records "
                "WHERE category IS NOT NULL"
            )
            rows = await cursor.fetchall()

        out: dict[str, Any] = {}
        for row in rows:
            entry = out.setdefault(row["category"], {"count": 0, "edited": 0, "samples": []})
            entry["count"] += 1
            was_edited = bool(row["was_edited"])
            if was_edited:
                entry["edited"] += 1
            if row["confidence"] is not None:
                entry["samples"].append((float(row["confidence"]), was_edited))
        for entry in out.values():
            entry["edit_rate"] = (
                round(entry["edited"] / entry["count"], 3) if entry["count"] else None
            )
        return out

    async def get_calibration_report(self) -> dict[str, Any]:
        """Confidence calibration: buckets past AI drafts by their confidence score and shows
        the edit rate within each bucket. A well-calibrated model's high-confidence bucket
        should have a LOW edit rate — if 0.85-0.95-confidence drafts get edited as often as
        0.5-0.6-confidence ones, the model's confidence number isn't trustworthy and
        AUTO_SEND_MIN_CONFIDENCE needs to be raised (or the model needs better prompting)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM edit_records WHERE confidence IS NOT NULL")
            rows = await cursor.fetchall()

        buckets = [
            (0.0, 0.5),
            (0.5, 0.6),
            (0.6, 0.7),
            (0.7, 0.8),
            (0.8, 0.85),
            (0.85, 0.9),
            (0.9, 1.01),
        ]
        report = {}
        for lo, hi in buckets:
            label = f"{lo:.2f}-{hi:.2f}" if hi <= 1.0 else f"{lo:.2f}-1.00"
            in_bucket = [r for r in rows if lo <= r["confidence"] < hi]
            if not in_bucket:
                report[label] = {"count": 0, "edit_rate": None}
                continue
            edited = sum(1 for r in in_bucket if r["was_edited"])
            report[label] = {
                "count": len(in_bucket),
                "edit_rate": round(edited / len(in_bucket), 3),
            }

        return {
            "buckets": report,
            "interpretation": (
                "A well-calibrated model shows DECREASING edit_rate as confidence increases. "
                "If a high bucket (0.85+) has a high edit_rate, confidence is not trustworthy "
                "there and AUTO_SEND_MIN_CONFIDENCE should be raised above that bucket."
            ),
            "sample_size_warning": (
                "Fewer than 30 samples in a bucket" if len(rows) < 30 else None
            ),
        }

    # ── Tracing (observability) ────────────────────────────────

    async def log_trace(
        self,
        ticket_id: str,
        stage: str,
        input_summary: dict[str, Any],
        output_summary: dict[str, Any],
        latency_ms: float | None = None,
        model: str | None = None,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        cost_usd: float | None = None,
        prompt_version: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO traces (ticket_id, stage, model, prompt_version, input_summary, output_summary, "
                "latency_ms, tokens_input, tokens_output, cost_usd, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ticket_id,
                    stage,
                    model,
                    prompt_version,
                    json.dumps(input_summary),
                    json.dumps(output_summary),
                    latency_ms,
                    tokens_input,
                    tokens_output,
                    cost_usd,
                    now,
                ),
            )
            await db.commit()

    async def get_traces(self, ticket_id: str) -> builtins.list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM traces WHERE ticket_id = ? ORDER BY id ASC", (ticket_id,)
            )
            rows = await cursor.fetchall()
        return [
            {
                "stage": r["stage"],
                "model": r["model"],
                "prompt_version": r["prompt_version"],
                "input_summary": json.loads(r["input_summary"]),
                "output_summary": json.loads(r["output_summary"]),
                "latency_ms": r["latency_ms"],
                "tokens_input": r["tokens_input"],
                "tokens_output": r["tokens_output"],
                "cost_usd": r["cost_usd"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ── Cost governance ─────────────────────────────────────────

    async def record_cost(
        self,
        ticket_id: str | None,
        stage: str,
        model: str,
        tokens_input: int,
        tokens_output: int,
        cost_usd: float,
    ) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO llm_costs (date, ticket_id, stage, model, tokens_input, tokens_output, "
                "cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (today, ticket_id, stage, model, tokens_input, tokens_output, cost_usd, now),
            )
            await db.commit()

    async def get_today_cost_usd(self) -> float:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_costs WHERE date = ?", (today,)
            )
            row = await cursor.fetchone()
            return round(row[0], 6) if row else 0.0

    async def get_cost_report(self, days: int = 7) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT date, SUM(cost_usd) as total, COUNT(*) as calls "
                "FROM llm_costs GROUP BY date ORDER BY date DESC LIMIT ?",
                (days,),
            )
            by_day = [
                {"date": r["date"], "cost_usd": round(r["total"], 6), "calls": r["calls"]}
                for r in await cursor.fetchall()
            ]

            cursor = await db.execute(
                "SELECT stage, SUM(cost_usd) as total, COUNT(*) as calls FROM llm_costs GROUP BY stage"
            )
            by_stage = [
                {"stage": r["stage"], "cost_usd": round(r["total"], 6), "calls": r["calls"]}
                for r in await cursor.fetchall()
            ]

        return {
            "today_usd": await self.get_today_cost_usd(),
            "by_day": by_day,
            "by_stage": by_stage,
        }

    # ── Chat sessions (storefront widget channel) ──────────────

    async def create_chat_session(self, session_id: str, data: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO chat_sessions (id, ticket_id, data, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, None, json.dumps(data), now, now),
            )
            await db.commit()
        return {
            "id": session_id,
            "ticket_id": None,
            "data": data,
            "created_at": now,
            "updated_at": now,
        }

    async def get_chat_session(self, session_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM chat_sessions WHERE id = ?", (session_id,))
            row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "ticket_id": row["ticket_id"],
            "data": json.loads(row["data"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def update_chat_session(
        self, session_id: str, merge: dict[str, Any] | None = None, ticket_id: str | None = None
    ) -> dict[str, Any] | None:
        """Merge fields into the session's data JSON and/or attach the ticket id."""
        existing = await self.get_chat_session(session_id)
        if not existing:
            return None
        data = existing["data"]
        if merge:
            data.update(merge)
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE chat_sessions SET data = ?, ticket_id = COALESCE(?, ticket_id), "
                "updated_at = ? WHERE id = ?",
                (json.dumps(data), ticket_id, now, session_id),
            )
            await db.commit()
        return await self.get_chat_session(session_id)

    # ── ROI aggregates (raw numbers; policy lives in agent/roi.py) ──

    async def get_roi_aggregates(self, since: str | None = None) -> dict[str, Any]:
        """Raw windowed counts behind the ROI dashboard. `since` is an ISO timestamp
        (created_at strings compare lexically). Returned uncomputed on purpose —
        agent/roi.py applies the store owner's time/cost assumptions so the math
        stays in one testable place."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            where = "WHERE created_at >= ?" if since else ""
            cursor = await db.execute(
                f"SELECT id, data, auto_sent, created_at FROM tickets {where} ORDER BY created_at ASC",
                (since,) if since else (),
            )
            tickets = await cursor.fetchall()

            cursor = await db.execute(
                "SELECT DISTINCT ticket_id FROM messages WHERE sender_type = 'agent'"
            )
            responded_ids = {r["ticket_id"] for r in await cursor.fetchall()}

            cursor = await db.execute("SELECT ticket_id, was_edited, category FROM edit_records")
            edits = await cursor.fetchall()

            cost_where = "WHERE date >= ?" if since else ""
            since_date = since[:10] if since else None
            cursor = await db.execute(
                f"SELECT COALESCE(SUM(cost_usd), 0) AS total FROM llm_costs {cost_where}",
                (since_date,) if since else (),
            )
            llm_cost = float((await cursor.fetchone())["total"])
            cursor = await db.execute(
                f"SELECT date, SUM(cost_usd) AS total FROM llm_costs {cost_where} GROUP BY date ORDER BY date",
                (since_date,) if since else (),
            )
            cost_by_day = {r["date"]: float(r["total"]) for r in await cursor.fetchall()}

        window_ids: set[str] = set()
        by_day: dict[str, dict[str, int]] = {}
        by_channel: dict[str, int] = {}
        category_tickets: dict[str, int] = {}
        auto_sent_count = 0
        for row in tickets:
            window_ids.add(row["id"])
            ticket = json.loads(row["data"])
            date = row["created_at"][:10]
            day = by_day.setdefault(date, {"tickets": 0, "auto_sent": 0, "responded": 0})
            day["tickets"] += 1
            if row["auto_sent"]:
                day["auto_sent"] += 1
                auto_sent_count += 1
            if row["id"] in responded_ids:
                day["responded"] += 1
            channel = ticket.get("channel") or "email"
            by_channel[channel] = by_channel.get(channel, 0) + 1
            category = ticket.get("category") or "unknown"
            category_tickets[category] = category_tickets.get(category, 0) + 1

        responded = len(window_ids & responded_ids)
        edited_ids = {e["ticket_id"] for e in edits if e["was_edited"]}
        edited = len(window_ids & edited_ids)

        category_edits: dict[str, dict[str, int]] = {}
        for e in edits:
            if e["ticket_id"] not in window_ids:
                continue
            cat = e["category"] or "unknown"
            bucket = category_edits.setdefault(cat, {"total": 0, "edited": 0})
            bucket["total"] += 1
            if e["was_edited"]:
                bucket["edited"] += 1

        return {
            "total": len(tickets),
            "auto_sent": auto_sent_count,
            "responded": responded,
            "edited": edited,
            "by_day": by_day,
            "by_channel": by_channel,
            "category_edits": category_edits,
            "category_tickets": category_tickets,
            "llm_cost_usd": round(llm_cost, 6),
            "llm_cost_by_day": cost_by_day,
        }

    @staticmethod
    def _row_to_dict(row) -> dict[str, Any]:
        return {
            "ticket": json.loads(row["data"]),
            "suggestion": json.loads(row["suggestion"]) if row["suggestion"] else None,
            "auto_sent": bool(row["auto_sent"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


def _text_similarity(a: str, b: str) -> float:
    """Cheap, dependency-free similarity: difflib ratio. Good enough to flag 'basically untouched'
    vs 'meaningfully rewritten' — not meant to be a precise NLP metric."""
    import difflib

    return difflib.SequenceMatcher(None, a.strip(), b.strip()).ratio()


store = TicketStore()
