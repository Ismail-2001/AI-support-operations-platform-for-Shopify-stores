"""Tests for evals/scoring.py — the eval harness's own correctness. A harness that always
says PASS is worse than no harness at all, so these tests feed it deliberately WRONG model
output and confirm it correctly flags failures, plus deliberately CORRECT output and confirms
it doesn't false-positive."""

from types import SimpleNamespace

from evals.scoring import CaseResult, score_case, summarize


def _classification(
    category="order_status", priority="normal", sentiment="neutral", extracted_order_number=None
):
    return SimpleNamespace(
        category=SimpleNamespace(value=category),
        priority=SimpleNamespace(value=priority),
        sentiment=SimpleNamespace(value=sentiment),
        extracted_order_number=extracted_order_number,
    )


def _suggestion(
    confidence=0.9,
    requires_human_review=False,
    suggested_response="Here is your answer.",
    suggested_action=None,
):
    return SimpleNamespace(
        confidence=confidence,
        requires_human_review=requires_human_review,
        suggested_response=suggested_response,
        suggested_action=suggested_action,
    )


def _action(action_type, operation=None):
    return SimpleNamespace(
        type=SimpleNamespace(value=action_type),
        subscription_operation=SimpleNamespace(value=operation) if operation else None,
    )


def test_correct_category_passes():
    case = {"id": "c1", "expected_category": "order_status"}
    result = score_case(case, _classification(category="order_status"), _suggestion())
    assert result.passed
    assert result.category_correct is True


def test_wrong_category_fails_with_clear_message():
    case = {"id": "c2", "expected_category": "refund"}
    result = score_case(case, _classification(category="order_status"), _suggestion())
    assert not result.passed
    assert result.category_correct is False
    assert any("category" in f for f in result.failures)


def test_category_as_list_accepts_any_match():
    case = {"id": "c3", "expected_category": ["returns", "refund"]}
    result = score_case(case, _classification(category="refund"), _suggestion())
    assert result.passed


def test_hallucinated_order_number_is_caught():
    """The model invented an order number the customer never gave — this is exactly the
    kind of hallucination an eval must catch, since it would look fine in a demo."""
    case = {"id": "c4", "expected_extracted_order_number": None}
    result = score_case(case, _classification(extracted_order_number="9999"), _suggestion())
    assert not result.passed
    assert any("hallucinated" in f for f in result.failures)


def test_overconfidence_is_caught():
    case = {"id": "c5", "max_confidence_expected": 0.6}
    result = score_case(case, _classification(), _suggestion(confidence=0.95))
    assert not result.passed
    assert any("overconfident" in f for f in result.failures)


def test_missing_required_human_review_is_caught():
    case = {"id": "c6", "requires_human_review_expected": True}
    result = score_case(case, _classification(), _suggestion(requires_human_review=False))
    assert not result.passed
    assert any("requires_human_review" in f for f in result.failures)


def test_forbidden_phrase_in_response_is_caught():
    """This is the prompt-injection defense check: if the model got tricked into confirming
    a fake refund, the eval must catch it, not just check the category label."""
    case = {"id": "c7", "response_checks": {"must_not_contain": ["refund has been processed"]}}
    result = score_case(
        case,
        _classification(),
        _suggestion(suggested_response="Sure! Your refund has been processed, all done."),
    )
    assert not result.passed
    assert any("forbidden phrase" in f for f in result.failures)


def test_missing_required_phrase_is_caught():
    case = {"id": "c8", "response_checks": {"must_contain_one_of": ["order number"]}}
    result = score_case(
        case, _classification(), _suggestion(suggested_response="Sure, happy to help!")
    )
    assert not result.passed


def test_case_with_no_expectations_always_passes():
    """A case with no assertions configured shouldn't spuriously fail."""
    case = {"id": "c9"}
    result = score_case(case, _classification(), _suggestion())
    assert result.passed


def test_expected_action_type_passes_when_model_suggests_it():
    case = {"id": "a1", "expected_action_type": "cancel_order"}
    result = score_case(
        case, _classification(), _suggestion(suggested_action=_action("cancel_order"))
    )
    assert result.passed


def test_expected_action_type_fails_when_model_suggests_nothing():
    case = {"id": "a2", "expected_action_type": "cancel_order"}
    result = score_case(case, _classification(), _suggestion(suggested_action=None))
    assert not result.passed
    assert any("suggested_action.type" in f for f in result.failures)


def test_expected_action_type_none_fails_when_model_suggests_action():
    """A delivered order must not get a cancel suggestion — this catches the model
    over-eagerly proposing actions it shouldn't."""
    case = {"id": "a3", "expected_action_type": "none"}
    result = score_case(
        case, _classification(), _suggestion(suggested_action=_action("cancel_order"))
    )
    assert not result.passed


def test_expected_action_type_as_list_accepts_any_match():
    case = {"id": "a4", "expected_action_type": ["refund", "resend_order"]}
    result = score_case(case, _classification(), _suggestion(suggested_action=_action("refund")))
    assert result.passed


def test_expected_action_operation_passes_when_matched():
    """pause expected, model proposed pause - the two fields (type vs operation)
    must be scored independently."""
    case = {"id": "o1", "expected_action_operation": "pause"}
    result = score_case(
        case,
        _classification(),
        _suggestion(suggested_action=_action("subscription_action", operation="pause")),
    )
    assert result.passed


def test_expected_action_operation_fails_when_model_proposes_the_wrong_one():
    """Skip asked, pause proposed - a wrong subscription operation is exactly the
    kind of near-miss that looks fine in a demo but changes customer data."""
    case = {"id": "o2", "expected_action_operation": "skip"}
    result = score_case(
        case,
        _classification(),
        _suggestion(suggested_action=_action("subscription_action", operation="pause")),
    )
    assert not result.passed
    assert any("subscription_operation" in f for f in result.failures)


def test_expected_action_operation_fails_when_model_proposes_nothing():
    case = {"id": "o3", "expected_action_operation": "cancel"}
    result = score_case(
        case, _classification(), _suggestion(suggested_action=_action("subscription_action"))
    )
    assert not result.passed
    assert any("subscription_operation" in f for f in result.failures)


def test_expected_action_operation_as_list_accepts_any_match():
    case = {"id": "o4", "expected_action_operation": ["pause", "skip"]}
    result = score_case(
        case,
        _classification(),
        _suggestion(suggested_action=_action("subscription_action", operation="skip")),
    )
    assert result.passed


def test_summarize_computes_pass_rate_and_category_accuracy():
    results = [
        CaseResult(case_id="a", passed=True, category_correct=True),
        CaseResult(case_id="b", passed=False, failures=["bad"], category_correct=False),
        CaseResult(case_id="c", passed=True, category_correct=True),
    ]
    summary = summarize(results)
    assert summary["total_cases"] == 3
    assert summary["passed"] == 2
    assert summary["pass_rate"] == round(2 / 3, 3)
    assert summary["category_accuracy"] == round(2 / 3, 3)
    assert summary["failed_case_ids"] == ["b"]


def test_summarize_handles_empty_results():
    summary = summarize([])
    assert summary["total_cases"] == 0
    assert summary["pass_rate"] is None
