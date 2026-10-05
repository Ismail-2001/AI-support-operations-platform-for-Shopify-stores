"""
Golden-dataset eval runner. Makes REAL calls to the configured LLM provider
(OpenRouter by default; needs OPENROUTER_API_KEY) against every case in
golden_dataset.json and scores the output — this is what tells you whether
a prompt change made things better or worse, instead of guessing from vibes.

Usage:
    python -m evals.run_evals
    python -m evals.run_evals --case order_status_clear   # run one case
    python -m evals.run_evals --json report.json           # also write a JSON report

After every full run, a copy is automatically saved to evals/results/
<timestamp>_<classifier_version>-<response_version>.json — commit these alongside
prompt changes so the evals/results/ directory builds a searchable history.

Run this:
  - Before merging any prompt change to classifier.py or response_engine.py
  - Weekly against production-derived cases (pull real tickets that got edited a lot —
    see /support/analytics/quality — and add them here as regression cases)
  - After upgrading the Gemini model version
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.classifier import PROMPT_VERSION as CLASSIFIER_VERSION
from agent.classifier import TicketClassifier
from agent.models import MessageSender, SupportTicket, TicketMessage
from agent.response_engine import PROMPT_VERSION as RESPONSE_VERSION
from agent.response_engine import ResponseGenerationEngine
from evals.scoring import score_case, summarize

DATASET_PATH = Path(__file__).parent / "golden_dataset.json"


def _build_history(messages: list) -> list[TicketMessage]:
    return [
        TicketMessage(ticket_id="eval", sender_type=MessageSender.CUSTOMER, content=m)
        for m in messages
    ]


async def run_case(
    case: dict, classifier: TicketClassifier, response_engine: ResponseGenerationEngine
) -> dict:
    history = _build_history(case["history"])
    ticket = SupportTicket(
        id=f"eval_{case['id']}",
        customer_email="eval@example.com",
        subject=case.get("subject", "Eval case"),
        body=case["history"][-1],
    )

    classification = await classifier.classify(ticket, history=history)

    # Real KB search is intentionally not wired in here — these cases test the
    # classifier/response prompts in isolation. Add a separate KB-integration eval suite
    # if you need to test retrieval quality end-to-end.
    knowledge_context = None

    suggestion = await response_engine.generate_suggestion(
        ticket,
        classification,
        order_context=None,
        knowledge_context=knowledge_context,
        # Optional per-case fixture: lets subscription cases exercise the real
        # prompt path with subscription data present (or absent, by omitting it).
        subscription_context=case.get("subscription_context"),
        # Same idea for return eligibility (return_label suggestion cases).
        return_context=case.get("return_context"),
        history=history,
    )

    result = score_case(case, classification, suggestion)
    action = suggestion.suggested_action
    action_type = getattr(action, "type", None) if action else None
    operation = getattr(action, "subscription_operation", None) if action else None
    return {
        "case_id": case["id"],
        "description": case.get("description", ""),
        "passed": result.passed,
        "failures": result.failures,
        "category_correct": result.category_correct,
        "actual_category": classification.category.value,
        "actual_priority": classification.priority.value,
        "actual_sentiment": classification.sentiment.value,
        "confidence": suggestion.confidence,
        "requires_human_review": suggestion.requires_human_review,
        "action_type": action_type.value if hasattr(action_type, "value") else action_type,
        "subscription_operation": operation.value if hasattr(operation, "value") else operation,
        "response_preview": suggestion.suggested_response[:200],
    }


async def main():
    parser = argparse.ArgumentParser(description="Run the golden-dataset eval suite")
    parser.add_argument("--case", help="Run only this case id")
    parser.add_argument("--json", help="Write full results to this JSON file")
    parser.add_argument(
        "--delay", type=float, default=4.0, help="Seconds to sleep between cases (default 4.0)"
    )
    args = parser.parse_args()

    dataset = json.loads(DATASET_PATH.read_text())
    if args.case:
        dataset = [c for c in dataset if c["id"] == args.case]
        if not dataset:
            print(f"No case found with id '{args.case}'")
            return

    classifier = TicketClassifier()
    response_engine = ResponseGenerationEngine()

    from agent.storage import store

    await store.init()

    print(f"Running {len(dataset)} eval case(s) against {classifier.model_name}...\n")

    results = []
    for i, case in enumerate(dataset):
        if i > 0 and args.delay > 0:
            await asyncio.sleep(args.delay)
        try:
            r = await run_case(case, classifier, response_engine)
        except Exception as e:
            r = {
                "case_id": case["id"],
                "passed": False,
                "failures": [f"EXCEPTION: {e}"],
                "description": case.get("description", ""),
            }
        results.append(r)
        status = "PASS" if r["passed"] else "FAIL"
        print(f"[{status}] {r['case_id']}: {r.get('description', '')}")
        for f in r.get("failures", []):
            print(f"       - {f}")

    from evals.scoring import CaseResult

    case_results = [
        CaseResult(
            case_id=r["case_id"],
            passed=r["passed"],
            failures=r.get("failures", []),
            category_correct=r.get("category_correct"),
            confidence=r.get("confidence"),
        )
        for r in results
    ]
    summary = summarize(case_results)
    print(f"\n{'=' * 60}")
    print(
        f"Pass rate: {summary['passed']}/{summary['total_cases']} ({summary['pass_rate']:.0%})"
        if summary["pass_rate"] is not None
        else "No cases run"
    )
    if summary["failed_case_ids"]:
        print(f"Failed: {', '.join(summary['failed_case_ids'])}")

    report = {
        "classifier_version": CLASSIFIER_VERSION,
        "response_version": RESPONSE_VERSION,
        "prompt_version": f"{CLASSIFIER_VERSION}-{RESPONSE_VERSION}",
        "summary": summary,
        "results": results,
    }

    # Auto-save to evals/results/ with a timestamped filename
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    auto_path = results_dir / f"{timestamp}_{CLASSIFIER_VERSION}-{RESPONSE_VERSION}.json"
    auto_path.write_text(json.dumps(report, indent=2))
    print(f"\nReport auto-saved to {auto_path}")

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(f"Full report also written to {args.json}")


if __name__ == "__main__":
    asyncio.run(main())
