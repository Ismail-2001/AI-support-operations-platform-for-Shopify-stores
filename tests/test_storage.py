"""Tests for storage-level features: edit-rate tracking (self-improvement signal) and
the refund idempotency/audit trail (security-critical — prevents double refunds)."""

import pytest

from agent.models import (
    ResponseSuggestion,
    Sentiment,
    SupportTicket,
    TicketCategory,
    TicketPriority,
)

pytestmark = pytest.mark.asyncio


# ── Edit tracking (self-improvement) ──────────────────────────


async def test_untouched_reply_is_not_counted_as_edited(test_store):
    await test_store.log_edit(
        "t1", "Your order shipped!", "Your order shipped!", category="order_status"
    )
    stats = await test_store.get_edit_stats()
    assert stats["total_ai_drafts_sent"] == 1
    assert stats["edited_before_send"] == 0


async def test_heavily_rewritten_reply_is_counted_as_edited(test_store):
    await test_store.log_edit(
        "t2",
        "Sorry, no refunds allowed.",
        "I hear you — let's get this refund started right away, sorry for the trouble!",
        category="refund",
    )
    stats = await test_store.get_edit_stats()
    assert stats["edited_before_send"] == 1
    assert stats["by_category"]["refund"]["edit_rate"] == 1.0


async def test_edit_stats_break_down_by_category(test_store):
    await test_store.log_edit("t1", "A", "A", category="order_status")  # untouched
    await test_store.log_edit(
        "t2", "B", "completely different text here", category="refund"
    )  # edited
    await test_store.log_edit("t3", "C", "C", category="order_status")  # untouched

    stats = await test_store.get_edit_stats()
    assert stats["by_category"]["order_status"]["total"] == 2
    assert stats["by_category"]["order_status"]["edited"] == 0
    assert stats["by_category"]["refund"]["total"] == 1
    assert stats["by_category"]["refund"]["edited"] == 1
    assert stats["overall_edit_rate"] == round(1 / 3, 3)


async def test_edit_stats_with_no_data_returns_none_rate(test_store):
    stats = await test_store.get_edit_stats()
    assert stats["total_ai_drafts_sent"] == 0
    assert stats["overall_edit_rate"] is None


# ── Analytics aggregation (regression: unawaited fetchall in _breakdown) ──


def _saved_ticket(ticket_id: str, category, priority, sentiment, status="open") -> SupportTicket:
    return SupportTicket(
        id=ticket_id,
        customer_email=f"{ticket_id}@example.com",
        subject="t",
        body="ticket body",
        status=status,
        category=category,
        priority=priority,
        sentiment=sentiment,
    )


async def test_analytics_aggregates_populated_store(test_store):
    """Saving real tickets then aggregating must produce correct totals and breakdowns.
    Regression test for the '/coroutine' object is not iterable' bug where
    cursor.fetchall() inside _breakdown was not awaited."""
    for i, (cat, prio, senti) in enumerate(
        [
            (TicketCategory.ORDER_STATUS, TicketPriority.HIGH, Sentiment.NEGATIVE),
            (TicketCategory.RETURNS, TicketPriority.NORMAL, Sentiment.NEUTRAL),
            (TicketCategory.PRODUCT_QUESTION, TicketPriority.LOW, Sentiment.POSITIVE),
        ]
    ):
        await test_store.save(
            _saved_ticket(f"t{i}", cat, prio, senti),
            suggestion=ResponseSuggestion(
                ticket_id=f"t{i}",
                suggested_response="draft",
                confidence=0.9,
                reasoning="test",
                requires_human_review=False,
            ),
            auto_sent=(i == 0),
        )

    agg = await test_store.analytics()
    assert agg["total"] == 3
    assert agg["auto_sent"] == 1
    assert agg["open"] == 3
    assert agg["category_breakdown"] == {
        "order_status": 1,
        "returns": 1,
        "product_question": 1,
    }
    assert agg["priority_breakdown"] == {"high": 1, "normal": 1, "low": 1}
    assert agg["sentiment_distribution"] == {"negative": 1, "neutral": 1, "positive": 1}
    assert agg["channel_breakdown"] == {"email": 3}


async def test_analytics_empty_store_returns_zeros(test_store):
    agg = await test_store.analytics()
    assert agg["total"] == 0
    assert agg["open"] == 0
    assert agg["auto_sent"] == 0
    assert agg["category_breakdown"] == {}


async def test_analytics_open_count_excludes_resolved(test_store):
    await test_store.save(
        _saved_ticket(
            "o1",
            TicketCategory.ORDER_STATUS,
            TicketPriority.NORMAL,
            Sentiment.NEUTRAL,
            status="open",
        )
    )
    await test_store.save(
        _saved_ticket(
            "o2",
            TicketCategory.RETURNS,
            TicketPriority.NORMAL,
            Sentiment.NEUTRAL,
            status="resolved",
        )
    )
    agg = await test_store.analytics()
    assert agg["total"] == 2
    assert agg["open"] == 1


# ── Refund idempotency / audit trail (security-critical) ──────


async def test_refund_audit_lookup_returns_none_before_any_refund(test_store):
    result = await test_store.get_refund_audit("key-1")
    assert result is None


async def test_refund_audit_records_and_replays_successful_refund(test_store):
    await test_store.record_refund_audit(
        "key-1",
        ticket_id="t1",
        order_id="999",
        amount=25.0,
        reason="damaged",
        status="succeeded",
        shopify_response={"refund": {"id": 555}},
    )
    replay = await test_store.get_refund_audit("key-1")
    assert replay is not None
    assert replay["amount"] == 25.0
    assert replay["status"] == "succeeded"
    assert replay["shopify_response"]["refund"]["id"] == 555


async def test_refund_audit_records_failures_too(test_store):
    await test_store.record_refund_audit(
        "key-2",
        ticket_id="t2",
        order_id="999",
        amount=25.0,
        reason="damaged",
        status="failed",
        error="Shopify API timeout",
    )
    replay = await test_store.get_refund_audit("key-2")
    assert replay["status"] == "failed"
    assert replay["error"] == "Shopify API timeout"


async def test_different_idempotency_keys_are_independent(test_store):
    await test_store.record_refund_audit(
        "key-a", ticket_id="t1", order_id="1", amount=10.0, reason="r", status="succeeded"
    )
    assert await test_store.get_refund_audit("key-b") is None
    assert (await test_store.get_refund_audit("key-a"))["amount"] == 10.0


# ── Resend order audit trail (security-critical) ───────────────


async def test_resend_audit_lookup_returns_none_before_any_resend(test_store):
    result = await test_store.get_resend_audit("resend-key-1")
    assert result is None


async def test_resend_audit_records_and_replays_successful_resend(test_store):
    await test_store.record_resend_audit(
        "resend-key-1",
        ticket_id="t1",
        order_id="999",
        status="succeeded",
        shopify_response={"new_order_id": 888, "original_order_id": "999"},
    )
    replay = await test_store.get_resend_audit("resend-key-1")
    assert replay is not None
    assert replay["status"] == "succeeded"
    assert replay["shopify_response"]["new_order_id"] == 888


async def test_resend_audit_records_failures_too(test_store):
    await test_store.record_resend_audit(
        "resend-key-2",
        ticket_id="t2",
        order_id="999",
        status="failed",
        error="Shopify API timeout",
    )
    replay = await test_store.get_resend_audit("resend-key-2")
    assert replay["status"] == "failed"
    assert replay["error"] == "Shopify API timeout"


async def test_different_resend_idempotency_keys_are_independent(test_store):
    await test_store.record_resend_audit(
        "resend-a", ticket_id="t1", order_id="1", status="succeeded"
    )
    assert await test_store.get_resend_audit("resend-b") is None
    assert (await test_store.get_resend_audit("resend-a"))["status"] == "succeeded"


# ── Webhook idempotency (cross-source isolation) ───────────────


async def test_processed_webhook_events_different_sources_same_event_id_are_independent(test_store):
    """Two different sources with the same event_id must NOT be treated as duplicates."""
    await test_store.record_processed_webhook_event("evt-42", "gorgias")
    assert await test_store.get_processed_webhook_event("evt-42", "gorgias") is not None
    assert await test_store.get_processed_webhook_event("evt-42", "shopify") is None


# ── Confidence calibration ─────────────────────────────────────


async def test_calibration_report_empty_when_no_data(test_store):
    report = await test_store.get_calibration_report()
    assert all(b["count"] == 0 for b in report["buckets"].values())


async def test_calibration_report_buckets_by_confidence_correctly(test_store):
    # Low confidence, heavily edited (expected — low confidence SHOULD get edited)
    await test_store.log_edit(
        "t1", "draft", "totally different text", category="refund", confidence=0.4
    )
    # High confidence, untouched (expected — high confidence SHOULD be trustworthy)
    await test_store.log_edit(
        "t2", "Your order shipped!", "Your order shipped!", category="shipping", confidence=0.92
    )

    report = await test_store.get_calibration_report()
    low_bucket = report["buckets"]["0.00-0.50"]
    high_bucket = report["buckets"]["0.90-1.00"]
    assert low_bucket["count"] == 1
    assert low_bucket["edit_rate"] == 1.0
    assert high_bucket["count"] == 1
    assert high_bucket["edit_rate"] == 0.0


async def test_calibration_report_flags_miscalibration(test_store):
    """If HIGH confidence drafts get edited just as often as low confidence ones, that's
    the exact pattern a senior engineer needs surfaced — this test proves the report
    actually surfaces it rather than averaging it away."""
    for i in range(3):
        await test_store.log_edit(
            f"hi{i}", "draft text", "completely rewritten reply", category="x", confidence=0.9
        )

    report = await test_store.get_calibration_report()
    high_bucket = report["buckets"]["0.90-1.00"]
    assert high_bucket["edit_rate"] == 1.0, (
        "a 100% edit rate at 0.9 confidence must be visible, not hidden"
    )


# -- Storage persistence (ephemeral detection) ----------------


async def test_storage_is_ephemeral_flags_render_and_tmp_paths():
    from agent.storage import storage_is_ephemeral

    # Render free plan (working dir, no attached disk) + temp dirs = ephemeral
    assert storage_is_ephemeral("/opt/render/project/src/cs_agent.db") is True
    assert storage_is_ephemeral("/tmp/cs_agent.db") is True
    assert storage_is_ephemeral("/var/tmp/cs_agent.db") is True
    # Attached disk path = persistent
    assert storage_is_ephemeral("/var/data/cs_agent.db") is False
    # Relative default resolved against a non-Render cwd = persistent
    assert storage_is_ephemeral("cs_agent.db") is False


async def test_ticket_store_creates_parent_directory(tmp_path):
    from agent.storage import TicketStore

    db_file = tmp_path / "attached-disk" / "cs_agent.db"
    store_obj = TicketStore(db_path=str(db_file))
    assert db_file.parent.is_dir(), "DB_PATH parent dir must exist before first connect"
    assert store_obj.db_path == str(db_file)


async def test_claim_refund_audit_is_exclusive_and_finalizes_via_upsert(test_store):
    """C1: exactly one caller can hold a key; record_* finalizes the claim row
    (upsert) instead of colliding with its PRIMARY KEY."""
    first = await test_store.claim_refund_audit("claim-1", "t1", "o1", 1.0, "r")
    second = await test_store.claim_refund_audit("claim-1", "t2", "o2", 2.0, "r")
    assert first is True
    assert second is False, "second claim on the same key must lose"

    held = await test_store.get_refund_audit("claim-1")
    assert held["status"] == "in_progress"

    await test_store.record_refund_audit(
        "claim-1",
        "t1",
        "o1",
        1.0,
        "r",
        status="succeeded",
        shopify_response={"id": 42},
        http_status=200,
    )
    done = await test_store.get_refund_audit("claim-1")
    assert done["status"] == "succeeded"
    assert done["shopify_response"] == {"id": 42}
    assert done["http_status"] == 200


async def test_failed_audit_stores_http_status_and_error_code(test_store):
    """H3: failure replays need the ORIGINAL status + machine code (v6 columns)."""
    await test_store.record_refund_audit(
        "fail-1",
        "t1",
        "o1",
        5.0,
        "r",
        status="failed",
        error="gateway declined",
        http_status=502,
        error_code="REFUND_FAILED",
    )
    row = await test_store.get_refund_audit("fail-1")
    assert row["status"] == "failed"
    assert row["http_status"] == 502
    assert row["error_code"] == "REFUND_FAILED"
