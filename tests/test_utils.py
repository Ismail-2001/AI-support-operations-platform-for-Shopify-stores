"""Tests for agent/utils.py — shared utility functions."""

from agent.utils import redact_pii


class TestRedactPii:
    def test_redacts_email(self):
        result = redact_pii("Contact alice@example.com for help")
        assert "alice@example.com" not in result
        assert "[EMAIL REDACTED]" in result

    def test_redacts_multiple_emails(self):
        result = redact_pii("a@test.com and b@demo.org")
        assert "a@test.com" not in result
        assert "b@demo.org" not in result
        assert result.count("[EMAIL REDACTED]") == 2

    def test_redacts_us_phone_number(self):
        result = redact_pii("Call (555) 123-4567 for support")
        assert "(555) 123-4567" not in result
        assert "[PHONE REDACTED]" in result

    def test_redacts_phone_with_country_code(self):
        result = redact_pii("Call +1-555-123-4567")
        assert "+1-555-123-4567" not in result
        assert "[PHONE REDACTED]" in result

    def test_redacts_dashed_phone(self):
        result = redact_pii("Fax: 555-123-4567")
        assert "555-123-4567" not in result
        assert "[PHONE REDACTED]" in result

    def test_preserves_non_pii_text(self):
        text = "Your order #1042 is on its way!"
        result = redact_pii(text)
        assert result == text

    def test_redacts_email_and_phone_in_same_text(self):
        result = redact_pii("Email bob@test.com or call 555-123-4567")
        assert "[EMAIL REDACTED]" in result
        assert "[PHONE REDACTED]" in result
        assert "bob@test.com" not in result
        assert "555-123-4567" not in result

    def test_handles_empty_string(self):
        assert redact_pii("") == ""

    def test_does_not_redact_partial_matches(self):
        result = redact_pii("not-an-email@ and 123")
        assert "not-an-email@" in result
