"""SQLite-backed ticket store. Zero-config, file-based — fine for a single-instance deploy.

Swap for Supabase/Postgres once you're running multiple workers or need concurrent writers
(Render's free tier disk is ephemeral across deploys, so treat this as durable-enough for an
MVP, not as your permanent system of record).
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

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
"""


class TicketStore:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or settings.DB_PATH

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await self._migrate_traces(db)
            await db.commit()
        logger.info("ticket_store_ready", db_path=self.db_path)

    async def _migrate_traces(self, db):
        """Apply schema migrations in order."""
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

    async def save(
        self,
        ticket: SupportTicket,
        suggestion: Optional[ResponseSuggestion] = None,
        auto_sent: bool = False,
    ) -> None:
        now = datetime.utcnow().isoformat()
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

    async def get(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
            row = await cursor.fetchone()
            return self._row_to_dict(row) if row else None

    async def list(
        self,
        status: Optional[str] = None,
        category: Optional[str] = None,
        priority: Optional[str] = None,
        page: int = 1,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM tickets ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, (page - 1) * limit),
            )
            rows = await cursor.fetchall()
            results = [self._row_to_dict(r) for r in rows]

        if status:
            results = [r for r in results if r["ticket"].get("status") == status]
        if category:
            results = [r for r in results if r["ticket"].get("category") == category]
        if priority:
            results = [r for r in results if r["ticket"].get("priority") == priority]
        return results

    async def all(self) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tickets")
            rows = await cursor.fetchall()
            return [self._row_to_dict(r) for r in rows]

    async def analytics(self) -> Dict[str, Any]:
        """Aggregate ticket stats via SQL — avoids loading all rows into memory."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT COUNT(*) as total FROM tickets")
            total = (await cursor.fetchone())["total"]

            cursor = await db.execute(
                "SELECT COUNT(*) as cnt FROM tickets WHERE auto_sent = 1"
            )
            auto_sent = (await cursor.fetchone())["cnt"]

            cursor = await db.execute(
                """SELECT
                    json_extract(data, '$.status') as val, COUNT(*) as cnt
                   FROM tickets WHERE json_extract(data, '$.status') IN ('open', 'in_progress')
                   OR json_extract(data, '$.status') IS NULL GROUP BY val"""
            )
            open_count = sum(r["cnt"] for r in await cursor.fetchall())

            async def _breakdown(col: str) -> Dict[str, int]:
                cursor = await db.execute(
                    f"SELECT json_extract(data, '$.{col}') as val, COUNT(*) as cnt"
                    f" FROM tickets WHERE json_extract(data, '$.{col}') IS NOT NULL"
                    f" AND json_extract(data, '$.{col}') != '' GROUP BY val"
                )
                return {r["val"]: r["cnt"] for r in cursor.fetchall()}

            return {
                "total": total,
                "open": open_count,
                "auto_sent": auto_sent,
                "category_breakdown": await _breakdown("category"),
                "priority_breakdown": await _breakdown("priority"),
                "channel_breakdown": await _breakdown("channel"),
                "sentiment_distribution": await _breakdown("sentiment"),
            }

    async def update_status(self, ticket_id: str, **updates) -> Optional[Dict[str, Any]]:
        existing = await self.get(ticket_id)
        if not existing:
            return None
        ticket_data = existing["ticket"]
        ticket_data.update({k: v for k, v in updates.items() if v is not None})
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE tickets SET data = ?, updated_at = ? WHERE id = ?",
                (json.dumps(ticket_data), now, ticket_id),
            )
            await db.commit()
        return await self.get(ticket_id)

    async def get_ticket_model(self, ticket_id: str) -> Optional[SupportTicket]:
        """Rehydrate a full SupportTicket object from storage (for follow-up processing)."""
        row = await self.get(ticket_id)
        if not row:
            return None
        return SupportTicket(**row["ticket"])

    async def get_ticket_by_gorgias_id(self, gorgias_ticket_id: str) -> Optional[SupportTicket]:
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
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO messages (ticket_id, sender_type, content, created_at) VALUES (?, ?, ?, ?)",
                (ticket_id, sender_type, content, now),
            )
            await db.commit()
            message_id = cursor.lastrowid
        return TicketMessage(id=message_id, ticket_id=ticket_id, sender_type=sender_type, content=content, created_at=now)

    async def get_messages(self, ticket_id: str) -> List[TicketMessage]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM messages WHERE ticket_id = ? ORDER BY id ASC", (ticket_id,)
            )
            rows = await cursor.fetchall()
            return [
                TicketMessage(
                    id=r["id"], ticket_id=r["ticket_id"], sender_type=r["sender_type"],
                    content=r["content"], created_at=r["created_at"],
                )
                for r in rows
            ]

    async def log_edit(
        self, ticket_id: str, ai_suggestion: str, final_response: str,
        category: Optional[str] = None, confidence: Optional[float] = None,
    ) -> None:
        """Called every time a human sends a reply that started from an AI draft. Tracking how
        much humans change the AI's drafts, broken down by category, is the honest version of
        'self-improvement' for a system like this: it doesn't retrain itself, but it tells you
        exactly which categories need prompt work or more knowledge base content next."""
        similarity = _text_similarity(ai_suggestion, final_response)
        was_edited = similarity < 0.98
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO edit_records (ticket_id, ai_suggestion, final_response, was_edited, "
                "similarity, category, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ticket_id, ai_suggestion, final_response, int(was_edited), similarity, category, confidence, now),
            )
            await db.commit()

    async def get_edit_stats(self) -> Dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM edit_records")
            rows = await cursor.fetchall()

        total = len(rows)
        edited = sum(1 for r in rows if r["was_edited"])
        by_category: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            cat = r["category"] or "unknown"
            bucket = by_category.setdefault(cat, {"total": 0, "edited": 0, "avg_similarity": 0.0})
            bucket["total"] += 1
            bucket["edited"] += int(r["was_edited"])

        for cat, bucket in by_category.items():
            cat_rows = [r for r in rows if (r["category"] or "unknown") == cat]
            bucket["avg_similarity"] = round(sum(r["similarity"] for r in cat_rows) / len(cat_rows), 3)
            bucket["edit_rate"] = round(bucket["edited"] / bucket["total"], 3) if bucket["total"] else 0.0

        return {
            "total_ai_drafts_sent": total,
            "edited_before_send": edited,
            "overall_edit_rate": round(edited / total, 3) if total else None,
            "by_category": by_category,
        }

    async def get_refund_audit(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
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
                "shopify_response": json.loads(row["shopify_response"]) if row["shopify_response"] else None,
                "error": row["error"],
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
        shopify_response: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO refund_audit (idempotency_key, ticket_id, order_id, amount, reason, "
                "status, shopify_response, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    idempotency_key, ticket_id, order_id, amount, reason, status,
                    json.dumps(shopify_response) if shopify_response else None, error, now,
                ),
            )
            await db.commit()

    async def get_resend_audit(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
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
                "shopify_response": json.loads(row["shopify_response"]) if row["shopify_response"] else None,
                "error": row["error"],
                "created_at": row["created_at"],
            }

    async def record_resend_audit(
        self,
        idempotency_key: str,
        ticket_id: str,
        order_id: str,
        status: str,
        shopify_response: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO resend_audit (idempotency_key, ticket_id, order_id, status, "
                "shopify_response, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    idempotency_key, ticket_id, order_id, status,
                    json.dumps(shopify_response) if shopify_response else None, error, now,
                ),
            )
            await db.commit()

    async def get_processed_webhook_event(self, event_id: str, source: str) -> Optional[Dict[str, Any]]:
        """If this webhook event was already processed, return it so the caller can skip
        re-processing instead of duplicating work."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM processed_webhook_events WHERE event_id = ? AND source = ?", (event_id, source)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {"event_id": row["event_id"], "source": row["source"], "processed_at": row["processed_at"]}

    async def record_processed_webhook_event(self, event_id: str, source: str) -> None:
        """Mark a webhook event as successfully processed so redeliveries are ignored."""
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO processed_webhook_events (event_id, source, processed_at) VALUES (?, ?, ?)",
                (event_id, source, now),
            )
            await db.commit()

    async def get_calibration_report(self) -> Dict[str, Any]:
        """Confidence calibration: buckets past AI drafts by their confidence score and shows
        the edit rate within each bucket. A well-calibrated model's high-confidence bucket
        should have a LOW edit rate — if 0.85-0.95-confidence drafts get edited as often as
        0.5-0.6-confidence ones, the model's confidence number isn't trustworthy and
        AUTO_SEND_MIN_CONFIDENCE needs to be raised (or the model needs better prompting)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM edit_records WHERE confidence IS NOT NULL"
            )
            rows = await cursor.fetchall()

        buckets = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.85), (0.85, 0.9), (0.9, 1.01)]
        report = {}
        for lo, hi in buckets:
            label = f"{lo:.2f}-{hi:.2f}" if hi <= 1.0 else f"{lo:.2f}-1.00"
            in_bucket = [r for r in rows if lo <= r["confidence"] < hi]
            if not in_bucket:
                report[label] = {"count": 0, "edit_rate": None}
                continue
            edited = sum(1 for r in in_bucket if r["was_edited"])
            report[label] = {"count": len(in_bucket), "edit_rate": round(edited / len(in_bucket), 3)}

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
        self, ticket_id: str, stage: str, input_summary: Dict[str, Any], output_summary: Dict[str, Any],
        latency_ms: Optional[float] = None, model: Optional[str] = None,
        tokens_input: Optional[int] = None, tokens_output: Optional[int] = None, cost_usd: Optional[float] = None,
        prompt_version: Optional[str] = None,
    ) -> None:
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO traces (ticket_id, stage, model, prompt_version, input_summary, output_summary, "
                "latency_ms, tokens_input, tokens_output, cost_usd, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ticket_id, stage, model, prompt_version, json.dumps(input_summary), json.dumps(output_summary),
                 latency_ms, tokens_input, tokens_output, cost_usd, now),
            )
            await db.commit()

    async def get_traces(self, ticket_id: str) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM traces WHERE ticket_id = ? ORDER BY id ASC", (ticket_id,)
            )
            rows = await cursor.fetchall()
        return [
            {
                "stage": r["stage"], "model": r["model"],
                "prompt_version": r["prompt_version"],
                "input_summary": json.loads(r["input_summary"]),
                "output_summary": json.loads(r["output_summary"]),
                "latency_ms": r["latency_ms"], "tokens_input": r["tokens_input"],
                "tokens_output": r["tokens_output"], "cost_usd": r["cost_usd"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ── Cost governance ─────────────────────────────────────────

    async def record_cost(
        self, ticket_id: Optional[str], stage: str, model: str,
        tokens_input: int, tokens_output: int, cost_usd: float,
    ) -> None:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO llm_costs (date, ticket_id, stage, model, tokens_input, tokens_output, "
                "cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (today, ticket_id, stage, model, tokens_input, tokens_output, cost_usd, now),
            )
            await db.commit()

    async def get_today_cost_usd(self) -> float:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_costs WHERE date = ?", (today,)
            )
            row = await cursor.fetchone()
            return round(row[0], 6) if row else 0.0

    async def get_cost_report(self, days: int = 7) -> Dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT date, SUM(cost_usd) as total, COUNT(*) as calls "
                "FROM llm_costs GROUP BY date ORDER BY date DESC LIMIT ?", (days,)
            )
            by_day = [{"date": r["date"], "cost_usd": round(r["total"], 6), "calls": r["calls"]} for r in await cursor.fetchall()]

            cursor = await db.execute(
                "SELECT stage, SUM(cost_usd) as total, COUNT(*) as calls FROM llm_costs GROUP BY stage"
            )
            by_stage = [{"stage": r["stage"], "cost_usd": round(r["total"], 6), "calls": r["calls"]} for r in await cursor.fetchall()]

        return {
            "today_usd": await self.get_today_cost_usd(),
            "by_day": by_day,
            "by_stage": by_stage,
        }

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
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
