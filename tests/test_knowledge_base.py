"""Tests for the knowledge base: chunking, similarity search relevance, and the
delete-then-reingest replace pattern. Embeddings are mocked with a simple deterministic
bag-of-words vector so cosine similarity is meaningful without a real network call."""

import numpy as np
import pytest

import agent.knowledge_base as kb_module
from agent.knowledge_base import _chunk_text

pytestmark = pytest.mark.asyncio

_VOCAB = [
    "return",
    "refund",
    "shipping",
    "waterproof",
    "material",
    "days",
    "hoodie",
    "cotton",
    "wash",
]


async def _fake_embed(text: str):
    t = text.lower()
    vec = np.array([1.0 if w in t else 0.0 for w in _VOCAB])
    if vec.sum() == 0:
        vec = np.ones(len(_VOCAB)) * 0.01
    return vec.tolist()


@pytest.fixture(autouse=True)
def patch_embeddings(monkeypatch):
    monkeypatch.setattr(kb_module, "embed_text", _fake_embed)


def test_chunk_text_splits_long_documents():
    long_text = (
        ("Returns accepted within 30 days. " * 20) + "\n\n" + ("Shipping takes 5-7 days. " * 20)
    )
    chunks = _chunk_text(long_text, max_chars=400)
    assert len(chunks) >= 2
    assert all(len(c) <= 500 for c in chunks)  # some slack for paragraph joins


def test_chunk_text_handles_short_documents():
    chunks = _chunk_text("Short FAQ answer.")
    assert len(chunks) == 1
    assert chunks[0] == "Short FAQ answer."


async def test_search_returns_most_relevant_document(test_kb):
    await test_kb.ingest("policy:returns", "Return Policy", "Returns accepted within 30 days.")
    await test_kb.ingest(
        "product:hoodie", "Blue Hoodie", "Cotton material, not waterproof, machine wash."
    )

    results = await test_kb.search("is the hoodie waterproof?", top_k=2, min_score=0.1)
    assert results
    assert results[0].source == "product:hoodie"


async def test_search_discriminates_between_unrelated_documents(test_kb):
    await test_kb.ingest(
        "policy:returns", "Return Policy", "Returns accepted within 30 days, full refund issued."
    )
    await test_kb.ingest(
        "policy:shipping", "Shipping Policy", "Standard shipping takes 5-7 business days."
    )

    results = await test_kb.search("how do I get a refund", top_k=1, min_score=0.1)
    assert results
    assert results[0].source == "policy:returns"


async def test_search_returns_empty_when_kb_is_empty(test_kb):
    results = await test_kb.search("anything", top_k=3)
    assert results == []


async def test_delete_source_then_reingest_replaces_not_duplicates(test_kb):
    await test_kb.ingest("policy:returns", "Return Policy", "Old: 30 day returns.")
    await test_kb.ingest("product:hoodie", "Hoodie", "Cotton material.")
    assert await test_kb.count() == 2

    await test_kb.delete_source("policy:returns")
    await test_kb.ingest("policy:returns", "Return Policy", "New: 45 day returns.")

    assert await test_kb.count() == 2, "replacing a source should not duplicate its chunks"
