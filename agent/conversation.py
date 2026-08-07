"""Shared helper: turn a stored message thread into a transcript the LLM can read."""

from agent.models import TicketMessage

_LABELS = {
    "customer": "Customer",
    "agent": "Human Agent",
    "ai": "AI Agent",
}


def format_transcript(history: list[TicketMessage]) -> str:
    if not history:
        return ""
    lines = []
    for m in history:
        label = _LABELS.get(
            m.sender_type.value if hasattr(m.sender_type, "value") else m.sender_type, m.sender_type
        )
        lines.append(f"{label}: {m.content}")
    return "\n\n".join(lines)
