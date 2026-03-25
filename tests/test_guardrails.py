"""Tests for the guardrails engine."""

from __future__ import annotations

from guardrails import GuardrailConfig, GuardrailEngine, _check_ssrf


class TestSSRFProtection:
    def test_blocks_localhost(self):
        result = _check_ssrf("localhost")
        assert result is not None
        assert not result.allowed

    def test_blocks_loopback_ip(self):
        result = _check_ssrf("127.0.0.1")
        assert result is not None
        assert not result.allowed

    def test_blocks_private_10(self):
        result = _check_ssrf("10.0.0.1")
        assert result is not None
        assert not result.allowed

    def test_blocks_private_172(self):
        result = _check_ssrf("172.16.0.1")
        assert result is not None
        assert not result.allowed

    def test_blocks_private_192(self):
        result = _check_ssrf("192.168.1.1")
        assert result is not None
        assert not result.allowed

    def test_blocks_metadata_endpoint(self):
        result = _check_ssrf("169.254.169.254")
        assert result is not None
        assert not result.allowed

    def test_blocks_ipv6_loopback(self):
        result = _check_ssrf("::1")
        assert result is not None
        assert not result.allowed

    def test_allows_public_ip(self):
        result = _check_ssrf("93.184.216.34")
        assert result is None

    def test_allows_hostname(self):
        result = _check_ssrf("example.com")
        assert result is None


class TestDomainChecks:
    def test_blocks_default_domains(self):
        engine = GuardrailEngine()
        result = engine.check_url("https://mail.google.com/inbox")
        assert not result.allowed

    def test_allows_unblocked_domain(self):
        engine = GuardrailEngine()
        result = engine.check_url("https://example.com")
        assert result.allowed

    def test_allowlist_overrides_blocklist(self):
        config = GuardrailConfig(allowed_domains=["example.com"])
        engine = GuardrailEngine(config)
        # Allowed domain passes
        assert engine.check_url("https://example.com").allowed
        # Non-allowed domain is blocked (even if not in blocklist)
        assert not engine.check_url("https://other.com").allowed

    def test_wildcard_blocklist(self):
        engine = GuardrailEngine()
        result = engine.check_url("https://foo.bank.example")
        assert not result.allowed

    def test_ssrf_blocked_by_default(self):
        engine = GuardrailEngine()
        result = engine.check_url("http://127.0.0.1:8080/admin")
        assert not result.allowed
        assert "SSRF" in (result.reason or "")

    def test_ssrf_allowed_when_configured(self):
        config = GuardrailConfig(allow_private_networks=True)
        engine = GuardrailEngine(config)
        result = engine.check_url("http://127.0.0.1:8080/admin")
        assert result.allowed


class TestNavigationTracking:
    def test_tracks_visited_urls(self):
        engine = GuardrailEngine()
        engine.check_navigation("https://example.com")
        assert "https://example.com" in engine.urls_visited

    def test_enforces_url_limit(self):
        config = GuardrailConfig(max_urls_visited=2)
        engine = GuardrailEngine(config)
        assert engine.check_navigation("https://a.com").allowed
        assert engine.check_navigation("https://b.com").allowed
        result = engine.check_navigation("https://c.com")
        assert not result.allowed
        assert "Max URL limit" in (result.reason or "")

    def test_same_url_doesnt_count_twice(self):
        config = GuardrailConfig(max_urls_visited=2)
        engine = GuardrailEngine(config)
        engine.check_navigation("https://a.com")
        engine.check_navigation("https://a.com")
        assert engine.check_navigation("https://b.com").allowed


class TestActionClassification:
    def test_blocks_destructive_click(self):
        engine = GuardrailEngine()
        result = engine.check_action("click", {"selector": "text=Place Order"})
        assert not result.allowed
        assert "purchase" in (result.reason or "").lower() or "form_submit" in (result.reason or "")

    def test_allows_non_destructive_click(self):
        engine = GuardrailEngine()
        result = engine.check_action("click", {"selector": "#next-button"})
        assert result.allowed

    def test_allows_non_click_actions(self):
        engine = GuardrailEngine()
        result = engine.check_action("goto", {"url": "https://example.com"})
        assert result.allowed

    def test_disabled_categories(self):
        config = GuardrailConfig(blocked_action_categories=[])
        engine = GuardrailEngine(config)
        result = engine.check_action("click", {"selector": "text=Delete Account"})
        assert result.allowed


class TestErrorTracking:
    def test_consecutive_errors_trigger_stop(self):
        config = GuardrailConfig(max_consecutive_errors=2)
        engine = GuardrailEngine(config)
        assert engine.record_error() is None
        assert engine.record_error() is None
        result = engine.record_error()
        assert result is not None
        assert not result.allowed

    def test_success_resets_error_count(self):
        config = GuardrailConfig(max_consecutive_errors=2)
        engine = GuardrailEngine(config)
        engine.record_error()
        engine.record_error()
        engine.record_success()
        assert engine.record_error() is None  # Reset, so first error again


class TestGuardrailConfigFromDict:
    def test_creates_from_dict(self):
        config = GuardrailConfig.from_dict(
            {
                "max_urls_visited": 100,
                "blocked_action_categories": [],
            }
        )
        assert config.max_urls_visited == 100
        assert config.blocked_action_categories == []

    def test_ignores_unknown_keys(self):
        config = GuardrailConfig.from_dict(
            {
                "max_urls_visited": 10,
                "unknown_field": "ignored",
            }
        )
        assert config.max_urls_visited == 10

    def test_empty_dict_uses_defaults(self):
        config = GuardrailConfig.from_dict({})
        assert config.max_urls_visited == 50
        assert config.max_consecutive_errors == 5
