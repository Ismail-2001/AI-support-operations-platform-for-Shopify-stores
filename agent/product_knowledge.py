"""Turn a Shopify product payload into one rich knowledge-base document.

The old sync indexed `title + body_html` only — enough to answer "what is this
product," useless for "is it machine washable," "does it come in medium," or "is
the natural color still in stock." This module pulls every field a shopper actually
asks about: variants (with live-ish inventory), metafields (care/materials/sizing/
warranty), vendor/type/tags, and image alt text as a last-resort signal.

Everything is formatted as paragraph-separated text so the existing KB chunker
(800-char paragraphs) splits it sensibly without changes.
"""

import html
import json
import re
from typing import Any

LOW_STOCK_THRESHOLD = 5

_META_FIELD_LABELS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("care_instructions", "careinstruction", "washing", "wash_care", "care"), "Care instructions"),
    (("material", "fabric", "composition", "content"), "Materials"),
    (("size_guide", "sizeguide", "sizing", "fit_guide", "fit"), "Sizing guide"),
    (("warranty", "guarantee"), "Warranty"),
    (("shipping", "delivery"), "Shipping"),
    (("country_of_origin", "origin"), "Country of origin"),
)

_TAG_RE = re.compile(r"<[^>]+>")
_TAG_LIKE_RE = re.compile(r"</?[a-zA-Z][^>]*>")


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (key or "").strip().lower()).strip("_")


def strip_html(body_html: str | None) -> str:
    """Product descriptions are HTML — chunkers and LLMs want plain text."""
    if not body_html:
        return ""
    text = _TAG_RE.sub(" ", body_html)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def stock_status(quantity: int | None) -> str | None:
    """'In stock' / 'Low stock (N left)' / 'Out of stock'. None = unknown
    (variant has no inventory_quantity — API didn't return it)."""
    if quantity is None:
        return None
    if quantity <= 0:
        return "Out of stock"
    if quantity <= LOW_STOCK_THRESHOLD:
        return f"Low stock ({quantity} left)"
    return f"In stock ({quantity} available)"


def _variant_line(variant: dict[str, Any]) -> str:
    title = variant.get("title") or "Default"
    price = variant.get("price")
    qty = variant.get("inventory_quantity")
    status = stock_status(qty if isinstance(qty, int) else None)
    parts = [f"{title}"]
    if price:
        parts.append(f"${price}")
    if status:
        parts.append(status)
    return "- " + " — ".join(parts)


def _metafield_lines(metafields: list[dict[str, Any]] | None) -> list[str]:
    """Label known metafield keys with friendly names ('care_instructions' ->
    'Care instructions: …') and pass anything else through humanized."""
    lines: list[str] = []
    for mf in metafields or []:
        value = mf.get("value")
        if value in (None, ""):
            continue
        if isinstance(value, dict | list):
            value = json.dumps(value, ensure_ascii=False)
        value = str(value).strip()
        if not value:
            continue
        if _TAG_LIKE_RE.search(value):
            value = strip_html(value)
        if not value:
            continue
        key = _normalize_key(mf.get("key", ""))
        label = None
        for tokens, friendly in _META_FIELD_LABELS:
            if any(token in key for token in tokens):
                label = friendly
                break
        if label is None:
            label = (
                (mf.get("key") or "Detail").replace("_", " ").replace("-", " ").strip().capitalize()
            )
        # Keep one metafield answerable in a single chunk — cap runaway values.
        if len(value) > 600:
            value = value[:600].rstrip() + "…"
        lines.append(f"{label}: {value}")
    return lines


def product_source(product: dict[str, Any]) -> str:
    """KB source key — stable per product, used for hash-skip + pruning."""
    return f"product:{product.get('handle') or product.get('id')}"


def stock_snapshot(product: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-variant inventory for the live-stock endpoint (real-time check)."""
    out: list[dict[str, Any]] = []
    for variant in product.get("variants") or []:
        qty = variant.get("inventory_quantity")
        qty = qty if isinstance(qty, int) else None
        if qty is None:
            status = "unknown"
        elif qty <= 0:
            status = "out_of_stock"
        elif qty <= LOW_STOCK_THRESHOLD:
            status = "low_stock"
        else:
            status = "in_stock"
        out.append(
            {"variant": variant.get("title") or "Default", "quantity": qty, "status": status}
        )
    return out


def build_product_document(
    product: dict[str, Any], metafields: list[dict[str, Any]] | None = None
) -> str:
    """One paragraph-per-fact document ready for KB ingestion. Deterministic —
    identical product + metafields => identical doc => identical hash => sync skips."""
    paragraphs: list[str] = []

    title = (product.get("title") or "").strip()
    if title:
        paragraphs.append(title)

    description = strip_html(product.get("body_html"))
    if description:
        paragraphs.append(description)

    facts: list[str] = []
    if product.get("vendor"):
        facts.append(f"Vendor: {product['vendor']}")
    if product.get("product_type"):
        facts.append(f"Product type: {product['product_type']}")
    if product.get("tags"):
        tags = product["tags"]
        if isinstance(tags, list):
            tags = ", ".join(str(t) for t in tags)
        facts.append(f"Tags: {tags}")
    if product.get("id"):
        facts.append(f"Product ID: {product['id']}")
    if facts:
        paragraphs.append("\n".join(facts))

    variants = product.get("variants") or []
    if variants:
        lines = [_variant_line(v) for v in variants]
        paragraphs.append("Variants (name — price — availability):\n" + "\n".join(lines))

    meta_lines = _metafield_lines(metafields)
    if meta_lines:
        paragraphs.append("\n".join(meta_lines))

    alts = [
        img.get("alt")
        for img in product.get("images") or []
        if isinstance(img, dict) and img.get("alt")
    ]
    if alts:
        paragraphs.append("Image descriptions: " + " | ".join(alts)[:400])

    return "\n\n".join(p for p in paragraphs if p).strip()
