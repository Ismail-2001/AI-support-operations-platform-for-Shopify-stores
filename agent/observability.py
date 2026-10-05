"""
Shared instrumentation for every LLM call in the pipeline. Both classifier.py and
response_engine.py call record_llm_call() right after their .ainvoke() — this is the
single place that:
  1. Extracts real token usage from the raw LangChain message (when using
     with_structured_output(..., include_raw=True), the raw AIMessage carries usage_metadata).
  2. Computes cost from that usage (via cost_tracker's pricing table).
  3. Persists both a trace (for the /tickets/{id}/trace debug endpoint) and a cost record
     (for the daily cap check and /analytics/costs).

Deliberately NOT a full OpenTelemetry/LangSmith integration — this is the minimum a small
team needs to answer "why did it say that" and "are we about to blow the budget" without
adding an external service dependency. Swap for LangSmith/Langfuse if you outgrow this.
"""

from typing import Any

import structlog

from agent.config import settings
from agent.cost_tracker import MODEL_PRICING

logger = structlog.get_logger(__name__)


def _extract_usage(raw_message: Any) -> tuple[int, int]:
    if raw_message is None:
        return 0, 0
    usage = getattr(raw_message, "usage_metadata", None) or {}
    tokens_in = usage.get("input_tokens", 0) or 0
    tokens_out = usage.get("output_tokens", 0) or 0
    return tokens_in, tokens_out


def _compute_cost(tokens_in: int, tokens_out: int, model: str) -> float:
    pricing = MODEL_PRICING.get(model, MODEL_PRICING["gemini-2.0-flash"])
    return round((tokens_in * pricing["input"] + tokens_out * pricing["output"]) / 1_000_000, 6)


async def record_llm_call(
    ticket_id: str,
    stage: str,
    model: str,
    raw_message: Any,
    latency_ms: float,
    input_summary: dict[str, Any],
    output_summary: dict[str, Any],
    prompt_version: str | None = None,
) -> float:
    """Returns cost_usd for this call, in case the caller wants it (e.g. for a response)."""
    from agent.storage import store  # local import avoids a circular import at module load

    tokens_in, tokens_out = _extract_usage(raw_message)
    cost_usd = _compute_cost(tokens_in, tokens_out, model)

    await store.log_trace(
        ticket_id=ticket_id,
        stage=stage,
        model=model,
        input_summary=input_summary,
        output_summary=output_summary,
        latency_ms=round(latency_ms, 1),
        tokens_input=tokens_in,
        tokens_output=tokens_out,
        cost_usd=cost_usd,
        prompt_version=prompt_version,
    )
    if cost_usd > 0:
        await store.record_cost(
            ticket_id=ticket_id,
            stage=stage,
            model=model,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            cost_usd=cost_usd,
        )
    return cost_usd


async def check_daily_cost_cap() -> bool:
    """Returns True if today's spend is within budget (or no cap is set). False means the
    caller should force auto-send off for this run — see support_agent.py."""
    if settings.DAILY_COST_CAP_USD <= 0:
        return True
    from agent.storage import store

    today_cost = await store.get_today_cost_usd()
    within_budget = today_cost < settings.DAILY_COST_CAP_USD
    if not within_budget:
        logger.critical(
            "daily_cost_cap_exceeded",
            today_cost_usd=today_cost,
            cap_usd=settings.DAILY_COST_CAP_USD,
            action="auto_send force-disabled until a human investigates",
        )
        from agent.alerting import send_alert

        await send_alert(
            title="Daily cost cap exceeded",
            message=(
                f"Today's LLM spend is ${today_cost:.2f} (cap ${settings.DAILY_COST_CAP_USD:.2f}). "
                "Auto-send is force-disabled until the cap is raised or the day rolls over."
            ),
            severity="critical",
            dedupe_key="daily_cost_cap",
        )
    return within_budget


async def record_graph_step(ticket_id: str, node_name: str, latency_ms: float) -> None:
    """Log a graph node transition to the traces table so /tickets/{id}/trace shows
    the graph's execution path through every node, not just the two LLM calls."""
    from agent.storage import store

    await store.log_trace(
        ticket_id=ticket_id,
        stage=f"graph:{node_name}",
        input_summary={},
        output_summary={},
        latency_ms=round(latency_ms, 1),
    )
