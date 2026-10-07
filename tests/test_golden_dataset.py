"""Validation for the golden dataset itself + proof that run_case actually forwards
per-case fixtures (subscription_context / return_context) into the prompt path.

The scoring logic is covered in tests/test_eval_harness.py; this file guards the
*data* (a typo'd category or duplicate id would silently weaken every eval run) and
the fixture wiring (a fixture that never reaches the prompt makes a case look green
while testing nothing).
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from agent.models import ActionType, SubscriptionOperation, TicketCategory
from evals.run_evals import DATASET_PATH, run_case

VALID_CATEGORIES = {c.value for c in TicketCategory}
VALID_ACTIONS = {a.value for a in ActionType}
VALID_OPERATIONS = {o.value for o in SubscriptionOperation}
ALL_OPERATIONS = set(VALID_OPERATIONS)


def _cases() -> list[dict]:
    return json.loads(Path(DATASET_PATH).read_text(encoding="utf-8"))


def _case(case_id: str) -> dict:
    matches = [c for c in _cases() if c["id"] == case_id]
    assert matches, f"case '{case_id}' not found in golden dataset"
    return matches[0]


def _categories(case: dict) -> list[str]:
    value = case.get("expected_category")
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# Any one of these keys makes a case actually score something (adversarial cases,
# for example, score purely through response_checks + review expectations).
SCORING_KEYS = (
    "expected_category",
    "expected_action_type",
    "expected_action_operation",
    "expected_priority",
    "expected_sentiment",
    "expected_extracted_order_number",
    "response_checks",
    "requires_human_review_expected",
    "max_confidence_expected",
)


def test_every_case_has_required_shape():
    for case in _cases():
        cid = case.get("id", "<missing id>")
        assert isinstance(cid, str) and cid, f"case missing id: {case}"
        assert isinstance(case.get("description"), str) and case["description"], cid
        history = case.get("history")
        assert isinstance(history, list) and history, cid
        assert all(isinstance(m, str) and m.strip() for m in history), cid
        assert any(key in case for key in SCORING_KEYS), f"{cid}: case scores nothing"


def test_case_ids_are_unique():
    ids = [c["id"] for c in _cases()]
    assert len(ids) == len(set(ids)), (
        f"duplicate ids: {sorted({i for i in ids if ids.count(i) > 1})}"
    )


def test_expected_values_are_valid_enums():
    for case in _cases():
        cid = case["id"]
        for category in _categories(case):
            assert category in VALID_CATEGORIES, f"{cid}: unknown category '{category}'"
        action = case.get("expected_action_type")
        actions = action if isinstance(action, list) else [action]
        for a in actions:
            if a is None:  # key absent or null = no action expectation (scoring skips it too)
                continue
            assert a in VALID_ACTIONS, f"{cid}: unknown action type '{a}'"
        operation = case.get("expected_action_operation")
        if operation is not None:
            ops = operation if isinstance(operation, list) else [operation]
            for o in ops:
                assert o in VALID_OPERATIONS, f"{cid}: unknown subscription operation '{o}'"
            assert action == "subscription_action" or (
                isinstance(action, list) and "subscription_action" in action
            ), f"{cid}: expected_action_operation requires expected_action_type=subscription_action"


def test_subscription_operations_have_a_context_fixture():
    for case in _cases():
        if case.get("expected_action_operation") is not None:
            assert case.get("subscription_context"), (
                f"{case['id']}: operation case without subscription_context fixture"
            )


def test_all_five_subscription_operations_are_covered():
    covered: set[str] = set()
    for c in _cases():
        op = c.get("expected_action_operation")
        if op is None:
            continue
        covered.update(op if isinstance(op, list) else [op])
    assert ALL_OPERATIONS <= covered, f"missing ops: {ALL_OPERATIONS - covered}"


def test_return_label_case_has_return_context_fixture():
    cases = [c for c in _cases() if "return_label" in str(c.get("expected_action_type"))]
    assert cases, "no return_label eval case"
    for case in cases:
        assert case.get("return_context"), f"{case['id']}: return_label case without return_context"


def test_response_check_phrases_are_nonempty_strings():
    for case in _cases():
        checks = case.get("response_checks", {})
        for key in ("must_not_contain", "must_contain_one_of"):
            for phrase in checks.get(key, []):
                assert isinstance(phrase, str) and phrase.strip(), (
                    f"{case['id']}: empty {key} phrase"
                )


def _fake_classifier(category: str):
    async def classify(ticket, history=None):
        return SimpleNamespace(
            category=TicketCategory(category),
            priority=SimpleNamespace(value="normal"),
            sentiment=SimpleNamespace(value="neutral"),
            extracted_order_number=None,
        )

    return SimpleNamespace(classify=classify)


def _fake_engine(action_type: str, operation: str | None = None, response: str = "Here you go."):
    captured: dict = {}

    async def generate_suggestion(ticket, classification, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            confidence=0.9,
            requires_human_review=True,
            suggested_response=response,
            suggested_action=SimpleNamespace(
                type=SimpleNamespace(value=action_type),
                subscription_operation=(SimpleNamespace(value=operation) if operation else None),
            ),
        )

    return SimpleNamespace(generate_suggestion=generate_suggestion), captured


def test_run_case_forwards_return_context_fixture():
    case = _case("return_label_damaged_item_suggests_action")
    engine, captured = _fake_engine("return_label")
    result = asyncio.run(run_case(case, _fake_classifier("returns"), engine))
    assert captured.get("return_context") == case["return_context"]
    assert captured.get("subscription_context") is None
    assert result["passed"], result["failures"]
    assert result["action_type"] == "return_label"


def test_run_case_forwards_subscription_context_fixture():
    case = _case("subscription_cancel_request")
    engine, captured = _fake_engine("subscription_action", operation="cancel")
    result = asyncio.run(run_case(case, _fake_classifier("subscription"), engine))
    assert captured.get("subscription_context") == case["subscription_context"]
    assert captured.get("return_context") is None
    assert result["passed"], result["failures"]
    assert result["subscription_operation"] == "cancel"


def test_run_case_without_fixtures_passes_none():
    case = _case("order_status_clear")
    engine, captured = _fake_engine("none")
    asyncio.run(run_case(case, _fake_classifier("order_status"), engine))
    assert captured.get("subscription_context") is None
    assert captured.get("return_context") is None
