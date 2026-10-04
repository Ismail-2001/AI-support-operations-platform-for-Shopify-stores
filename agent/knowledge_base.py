"""
Lightweight local RAG knowledge base. No Pinecone/Weaviate needed for one client's
worth of FAQ + policy content — a few hundred chunks fits comfortably in memory,
and SQLite + numpy cosine similarity is zero extra infrastructure to run.

Swap for pgvector/Pinecone once a single client's knowledge base grows past a few
thousand chunks, or once you're serving many clients from one process.
"""

import json

import aiosqlite
import numpy as np
import structlog

from agent.config import settings
from agent.context_proxy import ContextProxy
from agent.llm import embed_text
from agent.models import KnowledgeChunk

logger = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_sources (
    source TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _chunk_text(text: str, max_chars: int = 800) -> list[str]:
    """Simple paragraph-aware chunking — good enough for FAQ/policy pages. Paragraphs that
    are themselves longer than max_chars (e.g. one long product description with no blank
    lines) get hard-split on sentence boundaries so no single chunk ever exceeds the limit."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for p in paragraphs:
        if len(p) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_paragraph(p, max_chars))
            continue
        if len(current) + len(p) + 2 <= max_chars:
            current = f"{current}\n\n{p}".strip()
        else:
            if current:
                chunks.append(current)
            current = p
    if current:
        chunks.append(current)
    return chunks or [text[:max_chars]]


def _split_long_paragraph(paragraph: str, max_chars: int) -> list[str]:
    """Splits an oversized paragraph on sentence boundaries, falling back to a hard
    character cut only if a single 'sentence' is itself longer than max_chars."""
    sentences = [
        s.strip()
        for s in paragraph.replace("! ", "!|").replace("? ", "?|").replace(". ", ".|").split("|")
        if s.strip()
    ]
    chunks: list[str] = []
    current = ""
    for s in sentences:
        if len(s) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(s[i : i + max_chars] for i in range(0, len(s), max_chars))
            continue
        if len(current) + len(s) + 1 <= max_chars:
            current = f"{current} {s}".strip()
        else:
            if current:
                chunks.append(current)
            current = s
    if current:
        chunks.append(current)
    return chunks


class KnowledgeBase:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or settings.DB_PATH

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def ingest(self, source: str, title: str, content: str) -> int:
        """Chunks + embeds + stores a document. Returns number of chunks created."""
        try:
            chunks = _chunk_text(content)
            async with aiosqlite.connect(self.db_path) as db:
                for chunk in chunks:
                    vector = await embed_text(chunk)
                    await db.execute(
                        "INSERT INTO kb_chunks (source, title, content, embedding, created_at) "
                        "VALUES (?, ?, ?, ?, datetime('now'))",
                        (source, title, chunk, json.dumps(vector)),
                    )
                await db.commit()
            logger.info("kb_ingested", source=source, title=title, chunks=len(chunks))
            return len(chunks)
        except RuntimeError as e:
            raise RuntimeError(
                f"Knowledge base requires GOOGLE_API_KEY to be configured: {e}"
            ) from e

    async def delete_source(self, source: str) -> None:
        """Re-ingesting a policy page should replace the old chunks, not duplicate it."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM kb_chunks WHERE source = ?", (source,))
            await db.execute("DELETE FROM kb_sources WHERE source = ?", (source,))
            await db.commit()

    async def get_source_hash(self, source: str) -> str | None:
        """Content hash of the last successful ingest for this source (None = never
        synced or source was deleted). Lets incremental sync skip unchanged docs
        and avoid re-paying embedding calls on a 500-product catalog."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT content_hash FROM kb_sources WHERE source = ?", (source,)
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_source_hash(self, source: str, content_hash: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO kb_sources (source, content_hash, updated_at) VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(source) DO UPDATE SET content_hash = excluded.content_hash, "
                "updated_at = excluded.updated_at",
                (source, content_hash),
            )
            await db.commit()

    async def list_sources(self, prefix: str = "") -> list[str]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT source FROM kb_sources WHERE source LIKE ? ORDER BY source",
                (f"{prefix}%",),
            )
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

    async def search(
        self, query: str, top_k: int = 3, min_score: float = 0.55
    ) -> list[KnowledgeChunk]:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT * FROM kb_chunks")
                rows = await cursor.fetchall()

            if not rows:
                return []

            query_vector = np.array(await embed_text(query))
            query_norm = np.linalg.norm(query_vector)
            if query_norm == 0:
                return []

            scored = []
            for row in rows:
                doc_vector = np.array(json.loads(row["embedding"]))
                doc_norm = np.linalg.norm(doc_vector)
                if doc_norm == 0:
                    continue
                similarity = float(np.dot(query_vector, doc_vector) / (query_norm * doc_norm))
                if similarity >= min_score:
                    scored.append((similarity, row))

            scored.sort(key=lambda x: x[0], reverse=True)
            return [
                KnowledgeChunk(
                    id=row["id"],
                    source=row["source"],
                    title=row["title"],
                    content=row["content"],
                    score=score,
                )
                for score, row in scored[:top_k]
            ]
        except RuntimeError as e:
            raise RuntimeError(
                f"Knowledge base requires GOOGLE_API_KEY to be configured: {e}"
            ) from e

    async def count(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM kb_chunks")
            row = await cursor.fetchone()
            return row[0] if row else 0


def _current_knowledge_base() -> KnowledgeBase | None:
    from agent.multistore import knowledge_base_for_current

    return knowledge_base_for_current()


knowledge_base = ContextProxy(KnowledgeBase(), _current_knowledge_base)
