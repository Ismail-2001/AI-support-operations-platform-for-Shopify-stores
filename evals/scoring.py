"""
Pure scoring functions for the golden-dataset eval harness. Deliberately separated from
run_evals.py (which makes real LLM calls) so the scoring logic itself can be unit-tested
with fake results — see tests/test_eval_harness.py. A senior engineer's first question
about any eval system is "how do you know the eval harness itself is correct?" — this
split is the answer.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    category_correct: bool | None = None
    confidence: float | None = None


def score_case(case: dict[str, Any], classification: Any, suggestion: Any) -> CaseResult:
    """classification: ClassificationResult, suggestion: ResponseSuggestion (or any object/
    dict with matching attributes — kept duck-typed so tests can pass plain fakes)."""
    failures: list[str] = []
    category_correct = None

    def _val(obj, name):
        v = getattr(obj, name, None)
        return v.value if hasattr(v, "value") else v

    actual_category = _val(classification, "category")
    expected_category = case.get("expected_category")
    if expected_category is not None:
        allowed = expected_category if isinstance(expected_category, list) else [expected_category]
        category_correct = actual_category in allowed
        if not category_correct:
            failures.append(f"category: expected one of {allowed}, got '{actual_category}'")

    actual_priority = _val(classification, "priority")
    expected_priority = case.get("expected_priority")
    if expected_priority is not None:
        allowed = expected_priority if isinstance(expected_priority, list) else [expected_priority]
        if actual_priority not in allowed:
            failures.append(f"priority: expected one of {allowed}, got '{actual_priority}'")

    actual_sentiment = _val(classification, "sentiment")
    expected_sentiment = case.get("expected_sentiment")
    if expected_sentiment is not None:
        allowed = (
            expected_sentiment if isinstance(expected_sentiment, list) else [expected_sentiment]
        )
        if actual_sentiment not in allowed:
            failures.append(f"sentiment: expected one of {allowed}, got '{actual_sentiment}'")

    if "expected_extracted_order_number" in case:
        expected_num = case["expected_extracted_order_number"]
        actual_num = getattr(classification, "extracted_order_number", None)
        if expected_num is not None and actual_num != expected_num:
            failures.append(
                f"extracted_order_number: expected '{expected_num}', got '{actual_num}'"
            )
        if expected_num is None and actual_num is not None:
            failures.append(
                f"extracted_order_number: expected None, got '{actual_num}' (hallucinated an order number)"
            )

    confidence = getattr(suggestion, "confidence", None)
    max_conf = case.get("max_confidence_expected")
    if max_conf is not None and confidence is not None and confidence > max_conf:
        failures.append(f"confidence: expected <= {max_conf}, got {confidence} (overconfident)")

    requires_review_expected = case.get("requires_human_review_expected")
    actual_requires_review = getattr(suggestion, "requires_human_review", None)
    if requires_review_expected is not None and actual_requires_review != requires_review_expected:
        failures.append(
            f"requires_human_review: expected {requires_review_expected}, got {actual_requires_review}"
        )

    response_text = (getattr(suggestion, "suggested_response", "") or "").lower()
    checks = case.get("response_checks", {})
    for forbidden in checks.get("must_not_contain", []):
        if forbidden.lower() in response_text:
            failures.append(f"response contains forbidden phrase: '{forbidden}'")
    required_one_of = checks.get("must_contain_one_of")
    if required_one_of and not any(phrase.lower() in response_text for phrase in required_one_of):
        failures.append(f"response must contain one of {required_one_of}, found none")

    return CaseResult(
        case_id=case["id"],
        passed=(len(failures) == 0),
        failures=failures,
        category_correct=category_correct,
        confidence=confidence,
    )


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    category_judged = [r for r in results if r.category_correct is not None]
    category_correct = sum(1 for r in category_judged if r.category_correct)

    return {
        "total_cases": total,
        "passed": passed,
        "pass_rate": round(passed / total, 3) if total else None,
        "category_accuracy": round(category_correct / len(category_judged), 3)
        if category_judged
        else None,
        "failed_case_ids": [r.case_id for r in results if not r.passed],
    }
