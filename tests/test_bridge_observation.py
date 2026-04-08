"""Tests for DOM snapshot sanitization helpers."""

from bridge.observation import _redact_sensitive_form_values


def test_redact_sensitive_form_values_masks_credentials():
    snapshot = "\n".join(
        [
            '<input name="username" aria-label="username" value="alice@example.com">',
            '<input type="password" name="password" value="sup3r-secret">',
            '<textarea name="otp_code" value="123456">',
        ]
    )

    result = _redact_sensitive_form_values(snapshot)

    assert 'value="alice@example.com"' not in result
    assert 'value="sup3r-secret"' not in result
    assert 'value="123456"' not in result
    assert result.count('value="[redacted]"') == 3


def test_redact_sensitive_form_values_preserves_non_sensitive_fields():
    snapshot = "\n".join(
        [
            '<input name="search" value="running shoes">',
            '<textarea name="notes" value="leave at front desk">',
        ]
    )

    result = _redact_sensitive_form_values(snapshot)

    assert result == snapshot
