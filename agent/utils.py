"""Shared utilities for the agent package."""

import re


def redact_pii(text: str) -> str:
    """Mask emails and phone numbers before sending to the LLM.
    The LLM doesn't need real PII — it only needs the structure."""
    text = re.sub(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "[EMAIL REDACTED]", text)
    text = re.sub(r"(\+?1?[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", "[PHONE REDACTED]", text)
    return text
