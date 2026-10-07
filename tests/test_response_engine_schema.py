"""Regression: LLM structured-output schema must be provider-safe.

Once (2026-10-07) a `dict[str, ...]` field made Pydantic emit an OPEN map
(`additionalProperties` truthy) in `_RawSuggestion`'s JSON schema — OpenAI and
Azure both rejected the `response_format`, so EVERY live ticket 400'd until the
dict fields were replaced with typed models. These tests pin both invariants:
the schema never grows open maps again, and the typed models convert into
`SuggestedAction` payloads correctly (model_dump with exclude_none)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from agent.models import (
    ActionType,
    ClassificationResult,
    Sentiment,
    SupportTicket,
    TicketCategory,
    TicketPriority,
)
from agent.response_engine import (
    ResponseGenerationEngine,
    _RawAddress,
    _RawRefundLineItem,
    _RawSuggestedAction,
    _RawSuggestion,
)


def _find_open_maps(node, path="$"):
    """Paths of any schema node whose additionalProperties is an open map
    (True or a sub-schema) — OpenAI rejects those in structured outputs."""
    hits = []
    if isinstance(node, dict):
        ap = node.get("additionalProperties")
        if ap is not None and ap is not False:
            hits.append(f"{path}/additionalProperties={ap!r}")
        for key, value in node.items():
            hits += _find_open_maps(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            hits += _find_open_maps(value, f"{path}[{i}]")
    return hits


def test_raw_suggestion_schema_contains_no_open_maps():
    schema = _RawSuggestion.model_json_schema()
    assert _find_open_maps(schema) == [], "provider rejects open additionalProperties maps"


def test_nested_action_fields_are_typed_models():
    schema = _RawSuggestion.model_json_schema()
    defs = schema.get("$defs", {})
    for model_name in (
        "_RawAddress",
        "_RawFrequency",
        "_RawRefundLineItem",
        "_RawReturnLineItem",
        "_RawSuggestedAction",
    ):
        assert model_name in defs, f"{model_name} missing — a dict[] field regressed?"
    # The conversion path (model_dump exclude_none) must not leak raw dicts.
    action = _RawSuggestedAction(type="refund", address=_RawAddress(city="X"))
    assert isinstance(action.address, _RawAddress)
    assert action.model_dump(exclude_none=True)["address"] == {"city": "X"}


def test_raw_suggestion_json_schema_serializes():
    # The exact artifact handed to response_format must be valid JSON schema.
    json.dumps(_RawSuggestion.model_json_schema())


async def test_raw_suggestion_converts_to_typed_action():
    parsed = _RawSuggestion(
        suggested_response="Refund issued for the damaged hoodie.",
        confidence=0.93,
        reasoning="Customer reported damage within window.",
        requires_human_review=False,
        suggested_action=_RawSuggestedAction(
            type="refund",
            amount=12.5,
            reason="damaged item",
            address=_RawAddress(
                address1="1 Main St",
                address2=None,
                city="Springfield",
                zip="12345",
                country="US",
            ),
            refund_line_items=[_RawRefundLineItem(line_item_id=111, quantity=1)],
        ),
    )

    with (
        patch("agent.response_engine.get_llm") as get_llm,
        patch("agent.response_engine.get_fallback_llm", return_value=None),
        patch(
            "agent.response_engine.invoke_with_fallback",
            AsyncMock(return_value=({"parsed": parsed}, "test-model")),
        ),
        patch("agent.response_engine.record_llm_call", AsyncMock()),
        patch(
            "agent.response_engine.get_voice",
            AsyncMock(
                return_value={
                    "store_name": "",
                    "sign_off": "",
                    "support_email": "",
                    "tone": "friendly",
                }
            ),
        ),
    ):
        get_llm.return_value.with_structured_output.return_value = MagicMock()
        engine = ResponseGenerationEngine()
        ticket = SupportTicket(
            id="re-1", customer_email="a@b.com", subject="damaged", body="refund please"
        )
        classification = ClassificationResult(
            category=TicketCategory.REFUND,
            priority=TicketPriority.HIGH,
            sentiment=Sentiment.NEGATIVE,
            reasoning="refund requested",
        )
        suggestion = await engine.generate_suggestion(ticket, classification)

    action = suggestion.suggested_action
    assert action is not None
    assert action.type == ActionType.REFUND
    assert action.amount == 12.5
    assert action.refund_line_items == [{"line_item_id": 111, "quantity": 1}]
    # exclude_none: address2 and friends must not appear as null keys
    assert action.address == {
        "address1": "1 Main St",
        "city": "Springfield",
        "zip": "12345",
        "country": "US",
    }
    # Belt-and-suspenders: any suggested action forces human review.
    assert suggestion.requires_human_review is True


async def test_unknown_action_type_is_dropped_not_executed():
    """A model-invented action type must never map onto a real money action."""
    parsed = _RawSuggestion(
        suggested_response="Sure!",
        confidence=0.95,
        reasoning="ok",
        requires_human_review=False,
        suggested_action=_RawSuggestedAction(type="wire_crypto", amount=9000),
    )

    with (
        patch("agent.response_engine.get_llm") as get_llm,
        patch("agent.response_engine.get_fallback_llm", return_value=None),
        patch(
            "agent.response_engine.invoke_with_fallback",
            AsyncMock(return_value=({"parsed": parsed}, "test-model")),
        ),
        patch("agent.response_engine.record_llm_call", AsyncMock()),
        patch(
            "agent.response_engine.get_voice",
            AsyncMock(
                return_value={
                    "store_name": "",
                    "sign_off": "",
                    "support_email": "",
                    "tone": "friendly",
                }
            ),
        ),
    ):
        get_llm.return_value.with_structured_output.return_value = MagicMock()
        engine = ResponseGenerationEngine()
        ticket = SupportTicket(id="re-2", customer_email="a@b.com", subject="s", body="hi")
        classification = ClassificationResult(
            category=TicketCategory.OTHER,
            priority=TicketPriority.NORMAL,
            sentiment=Sentiment.NEUTRAL,
            reasoning="general",
        )
        suggestion = await engine.generate_suggestion(ticket, classification)

    assert suggestion.suggested_action is None
