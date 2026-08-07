"""Tests for prompt versioning: evals/compare.py diff logic and trace prompt_version
inclusion in observability records."""

import pytest

from evals.compare import compare, print_diff

pytestmark = pytest.mark.asyncio


# ── compare.py diffs ────────────────────────────────────────


def _report(prompt_version, pass_rate, category_accuracy, results):
    return {
        "classifier_version": prompt_version.split("-")[0]
        if "-" in prompt_version
        else prompt_version,
        "response_version": prompt_version.split("-")[1] if "-" in prompt_version else "unknown",
        "prompt_version": prompt_version,
        "summary": {
            "pass_rate": pass_rate,
            "category_accuracy": category_accuracy,
            "total_cases": len(results),
            "passed": sum(1 for r in results if r["passed"]),
            "failed_case_ids": [r["case_id"] for r in results if not r["passed"]],
        },
        "results": results,
    }


def _result(case_id, passed, failures=None, category_correct=None):
    return {
        "case_id": case_id,
        "passed": passed,
        "failures": failures or [],
        "category_correct": category_correct,
        "description": f"Test case {case_id}",
        "confidence": 0.9,
    }


class TestCompareReports:
    def test_identical_reports_no_flips(self):
        r = _report(
            "v1-v1",
            0.8,
            0.9,
            [
                _result("a", True),
                _result("b", False, ["bad"]),
            ],
        )
        diff = compare(r, r)
        assert diff["total_flipped"] == 0
        assert diff["pass_rate_delta"] == 0.0

    def test_one_case_flips_from_fail_to_pass(self):
        before = _report(
            "v1-v1",
            0.5,
            0.5,
            [
                _result("a", True),
                _result("b", False, ["wrong category"]),
            ],
        )
        after = _report(
            "v2-v1",
            1.0,
            1.0,
            [
                _result("a", True),
                _result("b", True),
            ],
        )
        diff = compare(before, after)
        assert diff["total_flipped"] == 1
        assert diff["flipped_cases"][0]["case_id"] == "b"
        assert diff["flipped_cases"][0]["passed_before"] is False
        assert diff["flipped_cases"][0]["passed_after"] is True
        assert diff["pass_rate_delta"] == 0.5

    def test_one_case_flips_from_pass_to_fail(self):
        before = _report("v1-v1", 1.0, 1.0, [_result("c", True)])
        after = _report("v2-v1", 0.0, 0.0, [_result("c", False, ["overconfident"])])
        diff = compare(before, after)
        assert diff["total_flipped"] == 1
        assert diff["flipped_cases"][0]["passed_before"] is True
        assert diff["flipped_cases"][0]["passed_after"] is False
        assert diff["pass_rate_delta"] == -1.0

    def test_category_accuracy_delta(self):
        before = _report(
            "v1-v1",
            0.5,
            0.5,
            [
                _result("a", True, category_correct=True),
                _result("b", False, ["bad"], category_correct=False),
            ],
        )
        after = _report(
            "v2-v1",
            1.0,
            1.0,
            [
                _result("a", True, category_correct=True),
                _result("b", True, category_correct=True),
            ],
        )
        diff = compare(before, after)
        assert diff["category_accuracy_delta"] == 0.5

    def test_versions_in_report(self):
        before = _report("classifier_v1-response_v1", 1.0, 1.0, [_result("x", True)])
        after = _report("classifier_v2-response_v1", 1.0, 1.0, [_result("x", True)])
        diff = compare(before, after)
        assert diff["report_a"]["versions"]["prompt"] == "classifier_v1-response_v1"
        assert diff["report_b"]["versions"]["prompt"] == "classifier_v2-response_v1"

    def test_case_added_between_runs(self):
        before = _report("v1-v1", 1.0, 1.0, [_result("a", True)])
        after = _report("v2-v1", 1.0, 1.0, [_result("a", True), _result("b", True)])
        diff = compare(before, after)
        assert diff["total_flipped"] == 1
        assert diff["flipped_cases"][0]["change"] == "added"

    def test_no_flips_when_both_empty(self):
        diff = compare(_report("v1-v1", None, None, []), _report("v2-v1", None, None, []))
        assert diff["total_flipped"] == 0
        assert diff["pass_rate_delta"] is None

    def test_print_diff_does_not_crash(self):
        """Smoke test: print_diff handles valid and edge-case input without error."""
        diff = compare(
            _report(
                "v1-v1",
                0.8,
                0.75,
                [
                    _result("a", True),
                    _result("b", False, ["bad"]),
                ],
            ),
            _report(
                "v2-v1",
                0.9,
                0.85,
                [
                    _result("a", True),
                    _result("b", True),
                ],
            ),
        )
        import io
        import sys

        captured = io.StringIO()
        old = sys.stdout
        sys.stdout = captured
        try:
            print_diff(diff)
        finally:
            sys.stdout = old
        output = captured.getvalue()
        assert "Flipped cases" in output
        assert "b" in output


# ── Trace prompt_version ────────────────────────────────────


class _FakeUsageMessage:
    def __init__(self, input_tokens=100, output_tokens=50):
        self.usage_metadata = {"input_tokens": input_tokens, "output_tokens": output_tokens}


async def test_record_llm_call_persists_prompt_version(test_store):
    import agent.storage as storage_module

    storage_module.store = test_store

    from agent.observability import record_llm_call

    msg = _FakeUsageMessage(200, 100)
    await record_llm_call(
        ticket_id="pv-t1",
        stage="classification",
        model="gemini-2.0-flash",
        raw_message=msg,
        latency_ms=50.0,
        input_summary={"q": "hello"},
        output_summary={"a": "world"},
        prompt_version="classifier_v1",
    )
    traces = await test_store.get_traces("pv-t1")
    assert len(traces) == 1
    assert traces[0]["prompt_version"] == "classifier_v1"


async def test_record_llm_call_defaults_prompt_version_to_none(test_store):
    import agent.storage as storage_module

    storage_module.store = test_store

    from agent.observability import record_llm_call

    await record_llm_call(
        ticket_id="pv-t2",
        stage="classification",
        model="gemini-2.0-flash",
        raw_message=None,
        latency_ms=10.0,
        input_summary={},
        output_summary={},
    )
    traces = await test_store.get_traces("pv-t2")
    assert len(traces) == 1
    assert traces[0]["prompt_version"] is None


async def test_log_trace_migration_adds_column_to_existing_db(test_store):
    """Simulate a v1 database without prompt_version by creating a table manually,
    then verify _migrate_traces adds the column."""
    import aiosqlite

    db_path = test_store.db_path
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS traces_v1 ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, stage TEXT, model TEXT, "
            "input_summary TEXT, output_summary TEXT, latency_ms REAL, tokens_input INTEGER, "
            "tokens_output INTEGER, cost_usd REAL, created_at TEXT)"
        )
        await db.commit()

    # Verify _migrate_traces adds prompt_version
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("PRAGMA table_info(traces)")
        columns = [row[1] for row in await cursor.fetchall()]
        assert "prompt_version" in columns
