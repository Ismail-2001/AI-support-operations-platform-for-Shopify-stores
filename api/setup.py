"""Client-onboarding wizard endpoints.

A new client's setup is four steps — this router backs them:

1. POST /support/setup/shopify   — validate + save Shopify credentials
2. (reuse existing /support/knowledge-base/sync-shopify + /support/knowledge-base)
3. PUT  /support/setup/voice     — save brand voice
4. POST /support/setup/test      — ask a sample question, get a draft, persist nothing

GET /support/setup reports progress for all four steps so the wizard can resume.
"""

import uuid
from typing import Literal

import httpx
import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from agent.auth import verify_api_key
from agent.config import settings
from agent.knowledge_base import knowledge_base
from agent.models import SupportTicket
from agent.rate_limit import rate_limit_default
from agent.setup_store import (
    apply_shopify_to_settings,
    get_kv,
    get_voice,
    normalize_shop_domain,
    persist_shopify_to_env,
    set_kv,
    set_voice,
)
from api.errors import APIError, raise_unprocessable, raise_validation_error

logger = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/support/setup",
    tags=["setup"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit_default)],
)

TEST_DONE_KEY = "setup_test_done"


# ── Status ────────────────────────────────────────────────────────────────────


@router.get("")
async def setup_status():
    """Wizard progress — which steps are already done for this instance."""
    from api.customer_support import _agent

    voice = await get_voice()
    chunk_count = await knowledge_base.count()
    voice_set = any(voice[f] for f in ("store_name", "sign_off", "support_email"))
    shopify_connected = _agent.shopify.enabled
    test_done = await get_kv(TEST_DONE_KEY) == "1"

    return {
        "shopify": {
            "connected": shopify_connected,
            "domain": settings.SHOPIFY_SHOP_DOMAIN,
        },
        "knowledge_base": {"chunk_count": chunk_count},
        "voice": voice,
        "voice_set": voice_set,
        "google_key_set": bool(settings.GOOGLE_API_KEY),
        "test_done": test_done,
        "steps": {
            "shopify": shopify_connected,
            "policies": chunk_count > 0,
            "voice": voice_set,
            "test": test_done,
        },
        "setup_complete": shopify_connected and chunk_count > 0 and voice_set and test_done,
    }


# ── Step 1: Shopify credentials ───────────────────────────────────────────────


class ShopifyConnectRequest(BaseModel):
    shop_domain: str = Field(min_length=1, max_length=255)
    access_token: str = Field(min_length=1, max_length=255)

    @field_validator("shop_domain", "access_token")
    @classmethod
    def strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v


@router.post("/shopify")
async def connect_shopify(req: ShopifyConnectRequest):
    """Validates credentials live against the Shopify Admin API BEFORE saving,
    so the wizard can show a real error instead of failing later mid-ticket."""
    try:
        domain = normalize_shop_domain(req.shop_domain)
    except ValueError as e:
        raise_validation_error("shop_domain", str(e))

    url = f"https://{domain}/admin/api/{settings.SHOPIFY_API_VERSION}/shop.json"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url,
                headers={
                    "X-Shopify-Access-Token": req.access_token,
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError as e:
        raise APIError(
            code="SHOPIFY_UNREACHABLE",
            message=f"Could not reach {domain}: {e}",
            status=502,
        ) from e

    if resp.status_code in (401, 403):
        raise APIError(
            code="SHOPIFY_INVALID_CREDENTIALS",
            message="Shopify rejected the access token — check it has the "
            "Read API scopes and hasn't been revoked.",
            status=401,
        )
    if resp.status_code == 404:
        raise APIError(
            code="SHOPIFY_SHOP_NOT_FOUND",
            message=f"No Shopify store found at '{domain}'",
            status=404,
        )
    if resp.status_code >= 500:
        raise APIError(
            code="SHOPIFY_UNAVAILABLE",
            message="Shopify is temporarily unavailable — try again shortly.",
            status=502,
        )

    shop = resp.json().get("shop", {})

    persist_shopify_to_env(domain, req.access_token)
    apply_shopify_to_settings(domain, req.access_token)

    # Refresh the module-level agent so the running pipeline + sync endpoints
    # pick the new store up immediately (no restart).
    from api.customer_support import _agent
    from integrations.shopify import ShopifyClient

    _agent.shopify = ShopifyClient()
    _agent._graph = None  # graph captured the old client — rebuild on next use

    logger.info("setup_shopify_connected", shop_domain=domain, shop_name=shop.get("name"))
    return {
        "connected": True,
        "domain": domain,
        "shop_name": shop.get("name"),
        "shop_email": shop.get("email"),
        "currency": shop.get("currency"),
    }


# ── Step 3: brand voice ───────────────────────────────────────────────────────


class VoiceRequest(BaseModel):
    store_name: str = Field(default="", max_length=120)
    tone: Literal["friendly", "professional", "casual"] = "friendly"
    sign_off: str = Field(default="", max_length=200)
    support_email: str = Field(default="", max_length=200)

    @field_validator("store_name", "sign_off", "support_email")
    @classmethod
    def empty_to_blank(cls, v: str) -> str:
        return (v or "").strip()


@router.put("/voice")
async def save_voice(req: VoiceRequest):
    if req.support_email and ("@" not in req.support_email or " " in req.support_email):
        raise_validation_error("support_email", "must be a valid email address")
    try:
        voice = await set_voice(req.model_dump())
    except ValueError as e:
        raise_unprocessable(str(e))
    logger.info("setup_voice_saved", tone=voice["tone"], store_name=voice["store_name"])
    return {"voice": voice}


# ── Step 4: test draft ────────────────────────────────────────────────────────


class SetupTestRequest(BaseModel):
    question: str = Field(min_length=5, max_length=2000)

    @field_validator("question")
    @classmethod
    def strip(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 5:
            raise ValueError("question is too short")
        return v


@router.post("/test")
async def test_draft(req: SetupTestRequest):
    """Runs the full classify + draft pipeline on a sample question.
    Nothing is persisted — this is a live preview, not a real ticket."""
    from api.customer_support import _agent

    ticket = SupportTicket(
        id=f"setup_test_{uuid.uuid4().hex[:8]}",
        customer_email="setup-test@example.com",
        subject="Setup test",
        body=req.question,
    )
    try:
        decision = await _agent.dry_run(ticket)
    except Exception as e:
        # Free-tier quota exhaustion and provider outages are common here —
        # surface a clear, actionable message instead of a bare 500.
        logger.warning("setup_test_failed", error=str(e))
        raise APIError(
            code="LLM_UNAVAILABLE",
            message="The AI provider didn't respond - its quota may be exhausted "
            "or the provider is down. Try again in a minute, or switch provider "
            "keys in .env.",
            status=503,
        ) from e
    await set_kv(TEST_DONE_KEY, "1")

    return {
        "classification": {
            "category": decision.classification.category.value,
            "priority": decision.classification.priority.value,
            "sentiment": decision.classification.sentiment.value,
            "reasoning": decision.classification.reasoning,
        },
        "suggestion": decision.suggestion.model_dump(mode="json"),
        "order_context_used": decision.order_context_used,
        "kb_used": decision.kb_used,
    }
