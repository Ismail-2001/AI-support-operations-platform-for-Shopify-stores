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

# Sync status is per store: agencies run several stores in one process, and one
# store's counters/single-flight lock must not block or mislead another.
# `_state` is the default scope (kept as a module global so existing tests and
# single-store deployments behave exactly as before); store scopes get their
# own entry in `_store_states`, keyed by store id.
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
_store_states: dict[str, dict[str, Any]] = {}


def _fresh_state() -> dict[str, Any]:
    return {
        "status": "idle",
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


def _state_for(scope: str | None = None) -> dict[str, Any]:
    if scope is None:
        from agent.multistore import get_store_id

        scope = get_store_id()
    if scope is None:
        return _state
    st = _store_states.get(scope)
    if st is None:
        st = _fresh_state()
        _store_states[scope] = st
    return st


def reset_sync_state() -> None:
    """Test helper: drop per-store sync state and reset the default scope."""
    _store_states.clear()
    _state.update(_fresh_state())


def get_sync_status(scope: str | None = None) -> dict[str, Any]:
    return dict(_state_for(scope))


def is_active(scope: str | None = None) -> bool:
    return _state_for(scope)["status"] in ("scheduled", "running")


def start_sync(force: bool = False, scope: str | None = None) -> bool:
    """Claim the sync slot for this scope. Returns False if one is already
    scheduled/running (the endpoint then reports 'running' instead of
    double-spawning the job)."""
    state = _state_for(scope)
    if state["status"] in ("scheduled", "running"):
        return False
    state.update(
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


async def _sync_policy(
    kb, title: str, body: str, force: bool, state: dict[str, Any] | None = None
) -> int:
    state = state if state is not None else _state_for()
    source = f"policy:{title.lower().replace(' ', '-')}"
    if not force and await kb.get_source_hash(source) == _content_hash(body):
        return 0
    await kb.delete_source(source)
    chunks = await kb.ingest(source, title, body)
    await kb.set_source_hash(source, _content_hash(body))
    state["policies_updated"] += 1
    state["chunks_added"] += chunks
    return chunks


async def _sync_product(
    kb, shopify, product: dict[str, Any], force: bool, state: dict[str, Any] | None = None
) -> None:
    state = state if state is not None else _state_for()
    state["products_seen"] += 1
    source = product_source(product)
    try:
        metafields = await shopify.get_product_metafields(product.get("id", ""))
        doc = build_product_document(product, metafields)
        if not force and await kb.get_source_hash(source) == _content_hash(doc):
            state["products_skipped"] += 1
            return
        await kb.delete_source(source)
        chunks = await kb.ingest(source, product.get("title") or "Untitled product", doc)
        await kb.set_source_hash(source, _content_hash(doc))
        state["products_updated"] += 1
        state["chunks_added"] += chunks
    except Exception as e:
        state["products_failed"] += 1
        logger.warning("kb_sync_product_failed", source=source, error=str(e))


async def run_sync(force: bool = False, scope: str | None = None) -> None:
    """The job itself (run via FastAPI BackgroundTasks). Never raises - errors are
    captured in status so the dashboard can display them. `scope` defaults to the
    store id of the request that started the sync (context survives into the
    background task); pass it explicitly if you call this outside a request."""
    # Late imports: breaks the api → agent → api cycle and picks up the test
    # suite's reloaded api.customer_support module.
    import api.customer_support as cs
    from agent.knowledge_base import knowledge_base
    from integrations.shopify import ShopifyNotConfigured

    state = _state_for(scope)
    # Reset counters here (not just in start_sync) so a direct run_sync call is
    # self-contained and repeated runs don't accumulate state.
    state.update(
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
            await _sync_policy(knowledge_base, title, body, force, state)

        products = await shopify.get_products(limit=settings.PRODUCT_SYNC_LIMIT)
        for product in products:
            await _sync_product(knowledge_base, shopify, product, force, state)

        # Prune catalog sources that no longer exist upstream (deleted products).
        seen = {product_source(p) for p in products}
        for source in await knowledge_base.list_sources(prefix="product:"):
            if source not in seen:
                await knowledge_base.delete_source(source)

        if state["products_seen"] and state["products_failed"] == state["products_seen"]:
            state["error"] = "all products failed to sync (embedding key configured?)"
            state["status"] = "error"
        else:
            state["status"] = "idle"
        logger.info(
            "kb_sync_complete",
            **{
                k: state[k]
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
        state.update(status="error", error=str(e))
    except Exception as e:
        state.update(status="error", error=str(e))
        logger.error("kb_sync_failed", error=str(e), exc_info=True)
    finally:
        state["finished_at"] = _now()


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()
