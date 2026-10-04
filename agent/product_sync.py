"""Background Shopify → knowledge-base sync with incremental (hash-skip) ingestion.

Why background: a 500-product catalog is hundreds of API + embedding calls — tens
of seconds to minutes. The old synchronous endpoint held the HTTP request open and
died with it if the browser navigated away. Now POST returns immediately and the
dashboard polls /support/knowledge-base/sync-status.

Why incremental: embeddings cost money and rate limits are real. Each document's
sha256 is stored after ingest (kb_sources); unchanged docs are skipped entirely.
`force=True` (the UI's "Re-sync everything") bypasses hashes and re-embeds all.
"""

import hashlib
from datetime import UTC
from typing import Any

import structlog

from agent.config import settings
from agent.product_knowledge import build_product_document, product_source

logger = structlog.get_logger(__name__)

_state: dict[str, Any] = {
    "status": "idle",  # idle | scheduled | running | error
    "force": False,
    "products_seen": 0,
    "products_updated": 0,
    "products_skipped": 0,
    "products_failed": 0,
    "policies_updated": 0,
    "chunks_added": 0,
    "error": None,
    "started_at": None,
    "finished_at": None,
}


def get_sync_status() -> dict[str, Any]:
    return dict(_state)


def is_active() -> bool:
    return _state["status"] in ("scheduled", "running")


def start_sync(force: bool = False) -> bool:
    """Claim the sync slot. Returns False if one is already scheduled/running
    (the endpoint then reports 'running' instead of double-spawning the job)."""
    if is_active():
        return False
    _state.update(
        status="scheduled",
        force=force,
        products_seen=0,
        products_updated=0,
        products_skipped=0,
        products_failed=0,
        policies_updated=0,
        chunks_added=0,
        error=None,
        started_at=None,
        finished_at=None,
    )
    return True


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _sync_policy(kb, title: str, body: str, force: bool) -> int:
    source = f"policy:{title.lower().replace(' ', '-')}"
    if not force and await kb.get_source_hash(source) == _content_hash(body):
        return 0
    await kb.delete_source(source)
    chunks = await kb.ingest(source, title, body)
    await kb.set_source_hash(source, _content_hash(body))
    _state["policies_updated"] += 1
    _state["chunks_added"] += chunks
    return chunks


async def _sync_product(kb, shopify, product: dict[str, Any], force: bool) -> None:
    _state["products_seen"] += 1
    source = product_source(product)
    try:
        metafields = await shopify.get_product_metafields(product.get("id", ""))
        doc = build_product_document(product, metafields)
        if not force and await kb.get_source_hash(source) == _content_hash(doc):
            _state["products_skipped"] += 1
            return
        await kb.delete_source(source)
        chunks = await kb.ingest(source, product.get("title") or "Untitled product", doc)
        await kb.set_source_hash(source, _content_hash(doc))
        _state["products_updated"] += 1
        _state["chunks_added"] += chunks
    except Exception as e:
        _state["products_failed"] += 1
        logger.warning("kb_sync_product_failed", source=source, error=str(e))


async def run_sync(force: bool = False) -> None:
    """The job itself (run via FastAPI BackgroundTasks). Never raises — errors are
    captured in status so the dashboard can display them."""
    # Late imports: breaks the api → agent → api cycle and picks up the test
    # suite's reloaded api.customer_support module.
    import api.customer_support as cs
    from agent.knowledge_base import knowledge_base
    from integrations.shopify import ShopifyNotConfigured

    # Reset counters here (not just in start_sync) so a direct run_sync call is
    # self-contained and repeated runs don't accumulate state.
    _state.update(
        status="running",
        force=force,
        products_seen=0,
        products_updated=0,
        products_skipped=0,
        products_failed=0,
        policies_updated=0,
        chunks_added=0,
        error=None,
        started_at=_now(),
        finished_at=None,
    )
    shopify = cs._agent.shopify
    try:
        if not shopify.enabled:
            raise ShopifyNotConfigured("Shopify credentials not set in .env")

        policies = await shopify.get_shop_policies()
        for title, body in policies.items():
            await _sync_policy(knowledge_base, title, body, force)

        products = await shopify.get_products(limit=settings.PRODUCT_SYNC_LIMIT)
        for product in products:
            await _sync_product(knowledge_base, shopify, product, force)

        # Prune catalog sources that no longer exist upstream (deleted products).
        seen = {product_source(p) for p in products}
        for source in await knowledge_base.list_sources(prefix="product:"):
            if source not in seen:
                await knowledge_base.delete_source(source)

        if _state["products_seen"] and _state["products_failed"] == _state["products_seen"]:
            _state["error"] = "all products failed to sync (embedding key configured?)"
            _state["status"] = "error"
        else:
            _state["status"] = "idle"
        logger.info(
            "kb_sync_complete",
            **{
                k: _state[k]
                for k in (
                    "products_seen",
                    "products_updated",
                    "products_skipped",
                    "products_failed",
                    "chunks_added",
                )
            },
        )
    except ShopifyNotConfigured as e:
        _state.update(status="error", error=str(e))
    except Exception as e:
        _state.update(status="error", error=str(e))
        logger.error("kb_sync_failed", error=str(e), exc_info=True)
    finally:
        _state["finished_at"] = _now()


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()
