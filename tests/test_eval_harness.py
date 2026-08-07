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
    confidence=0.9, requires_human_review=False, suggested_response="Here is your answer."
):
    return SimpleNamespace(
        confidence=confidence,
        requires_human_review=requires_human_review,
        suggested_response=suggested_response,
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
