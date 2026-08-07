"""
Diff two eval report JSON files and show what changed between prompt versions.

Usage:
    python -m evals.compare evals/results/20240710_classifier_v1-response_v1.json \\
                           evals/results/20240711_classifier_v2-response_v1.json

Output: which case ids flipped, pass rate delta, category accuracy delta.
"""

import json
import sys
from pathlib import Path


def load_report(path: Path) -> dict:
    return json.loads(path.read_text())


def _results_by_id(report: dict) -> dict[str, dict]:
    return {r["case_id"]: r for r in report["results"]}


def compare(report_a: dict, report_b: dict) -> dict:
    results_a = _results_by_id(report_a)
    results_b = _results_by_id(report_b)

    all_ids = set(results_a) | set(results_b)

    flipped: list[dict] = []
    for cid in sorted(all_ids):
        r_a = results_a.get(cid)
        r_b = results_b.get(cid)
        if r_a is None or r_b is None:
            flipped.append(
                {
                    "case_id": cid,
                    "change": "added" if r_a is None else "removed",
                    "passed_before": r_a["passed"] if r_a else None,
                    "passed_after": r_b["passed"] if r_b else None,
                }
            )
        elif r_a["passed"] != r_b["passed"]:
            flipped.append(
                {
                    "case_id": cid,
                    "change": "flip",
                    "passed_before": r_a["passed"],
                    "passed_after": r_b["passed"],
                    "failures_before": r_a.get("failures", []),
                    "failures_after": r_b.get("failures", []),
                }
            )

    sa = report_a["summary"]
    sb = report_b["summary"]
    pass_rate_delta = None
    if sa["pass_rate"] is not None and sb["pass_rate"] is not None:
        pass_rate_delta = round(sb["pass_rate"] - sa["pass_rate"], 3)

    cat_acc_delta = None
    if sa["category_accuracy"] is not None and sb["category_accuracy"] is not None:
        cat_acc_delta = round(sb["category_accuracy"] - sa["category_accuracy"], 3)

    return {
        "report_a": {
            "path": getattr(report_a, "_path", "unknown"),
            "versions": {
                "classifier": report_a.get("classifier_version"),
                "response": report_a.get("response_version"),
                "prompt": report_a.get("prompt_version"),
            },
            "pass_rate": sa["pass_rate"],
            "category_accuracy": sa["category_accuracy"],
            "total_cases": sa["total_cases"],
        },
        "report_b": {
            "path": getattr(report_b, "_path", "unknown"),
            "versions": {
                "classifier": report_b.get("classifier_version"),
                "response": report_b.get("response_version"),
                "prompt": report_b.get("prompt_version"),
            },
            "pass_rate": sb["pass_rate"],
            "category_accuracy": sb["category_accuracy"],
            "total_cases": sb["total_cases"],
        },
        "flipped_cases": flipped,
        "pass_rate_delta": pass_rate_delta,
        "category_accuracy_delta": cat_acc_delta,
        "total_flipped": len(flipped),
    }


def print_diff(diff: dict) -> None:
    ra = diff["report_a"]
    rb = diff["report_b"]

    print("Comparing:")
    print(f"  A: {ra['path']}  (v{ra['versions']['prompt']})")
    print(f"  B: {rb['path']}  (v{rb['versions']['prompt']})")
    print()

    if diff["flipped_cases"]:
        print(f"Flipped cases ({diff['total_flipped']}):")
        print(f"  {'Case ID':<30} {'Before':<8} {'After':<8}  Detail")
        print(f"  {'─' * 30}  {'─' * 8} {'─' * 8}  ─────────────────")
        for fc in diff["flipped_cases"]:
            before = "PASS" if fc["passed_before"] else "FAIL"
            after = "PASS" if fc["passed_after"] else "FAIL"
            detail = ""
            if fc["change"] == "flip":
                detail = fc.get("failures_after", [])[:1]
            elif fc["change"] == "added":
                detail = "new case"
            elif fc["change"] == "removed":
                detail = "removed case"
            print(f"  {fc['case_id']:<30} {before:<8} {after:<8}  {detail}")
        print()
    else:
        print("No cases flipped — pass/fail status is identical between the two runs.\n")

    print("Summary deltas:")
    prd = diff["pass_rate_delta"]
    cad = diff["category_accuracy_delta"]
    print(
        f"  Pass rate:          {ra['pass_rate']} → {rb['pass_rate']}  ({_sign(prd)}{prd:.1%})"
        if prd is not None
        else "  Pass rate:          N/A"
    )
    print(
        f"  Category accuracy:  {ra['category_accuracy']} → {rb['category_accuracy']}  ({_sign(cad)}{cad:.1%})"
        if cad is not None
        else "  Category accuracy:  N/A"
    )


def _sign(val: float | None) -> str:
    if val is None:
        return ""
    return "+" if val > 0 else ""


def main():
    if len(sys.argv) != 3:
        print("Usage: python -m evals.compare <report_a.json> <report_b.json>")
        sys.exit(1)

    path_a = Path(sys.argv[1])
    path_b = Path(sys.argv[2])

    report_a = load_report(path_a)
    report_a["_path"] = str(path_a)
    report_b = load_report(path_b)
    report_b["_path"] = str(path_b)

    diff = compare(report_a, report_b)
    print_diff(diff)


if __name__ == "__main__":
    main()
