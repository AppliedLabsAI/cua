"""Tests for the guardrails engine."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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
    async def test_allows_non_click_actions(self):
        engine = GuardrailEngine()
        result = await engine.check_action("goto", {"url": "https://example.com"})
        assert result.allowed

    async def test_skip_llm_allows_all_clicks(self):
        """skip_llm=True means no destructive check — all clicks pass."""
        engine = GuardrailEngine()
        result = await engine.check_action(
            "click", {"selector": "text=Delete Account"}, skip_llm=True
        )
        assert result.allowed

    async def test_disabled_llm_check(self):
        """With LLM disabled intentionally, regex-only behavior stays enabled."""
        config = GuardrailConfig(enable_llm_action_check=False)
        engine = GuardrailEngine(config)
        # Use a selector that is ambiguous (bypasses both regex patterns)
        result = await engine.check_action(
            "click", {"selector": "text=Process Batch #42"}
        )
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
        config = GuardrailConfig.model_validate(
            {
                "max_urls_visited": 100,
                "enable_llm_action_check": False,
            }
        )
        assert config.max_urls_visited == 100
        assert config.enable_llm_action_check is False

    def test_ignores_unknown_keys(self):
        config = GuardrailConfig.model_validate(
            {
                "max_urls_visited": 10,
                "unknown_field": "ignored",
            }
        )
        assert config.max_urls_visited == 10

    def test_empty_dict_uses_defaults(self):
        config = GuardrailConfig.model_validate({})
        assert config.max_urls_visited == 50
        assert config.max_consecutive_errors == 5


class TestHaikuDestructiveCheck:
    """Tests for Haiku-based destructive action detection."""

    async def test_skipped_when_disabled_in_config(self):
        """enable_llm_action_check=False disables the LLM layer."""
        config = GuardrailConfig(enable_llm_action_check=False)
        engine = GuardrailEngine(config)
        # Use ambiguous selector that bypasses regex
        result = await engine.check_action(
            "click", {"selector": "text=Finalize Changes"}
        )
        assert result.allowed

    async def test_skip_llm_flag(self):
        """skip_llm=True skips the Haiku check."""
        engine = GuardrailEngine()
        result = await engine.check_action(
            "click", {"selector": "text=Finalize Changes"}, skip_llm=True
        )
        assert result.allowed

    @patch("guardrails.engine._destructive_checker")
    async def test_flags_destructive_via_haiku_for_confirmation(self, mock_agent):
        """Haiku returning destructive=true flags for confirmation."""
        from guardrails.engine import DestructiveCheckResult

        mock_result = MagicMock()
        mock_result.output = DestructiveCheckResult(
            destructive=True, reason="confirms account deletion"
        )
        mock_result.usage.return_value = MagicMock(input_tokens=50, output_tokens=20)
        mock_agent.run = AsyncMock(return_value=mock_result)

        engine = GuardrailEngine()
        # Use ambiguous selector that bypasses regex → reaches Haiku
        result = await engine.check_action(
            "click", {"selector": "text=Proceed with action"}
        )
        assert not result.allowed
        assert result.needs_confirmation
        assert "LLM" in (result.reason or "")

    @patch("guardrails.engine._destructive_checker")
    async def test_blocks_ambiguous_click_when_haiku_unavailable(self, mock_agent):
        mock_agent.run = AsyncMock(side_effect=RuntimeError("network down"))

        engine = GuardrailEngine()
        result = await engine.check_action(
            "click", {"selector": "text=Proceed with action"}
        )
        assert not result.allowed
        assert not result.needs_confirmation
        assert "validation unavailable" in (result.reason or "")

    @patch("guardrails.engine._destructive_checker")
    async def test_retry_confirms_haiku_flagged_action(self, mock_agent):
        """Agent retry of Haiku-flagged action confirms and allows it."""
        from guardrails.engine import DestructiveCheckResult

        mock_result = MagicMock()
        mock_result.output = DestructiveCheckResult(
            destructive=True, reason="confirms deletion"
        )
        mock_result.usage.return_value = MagicMock(input_tokens=50, output_tokens=20)
        mock_agent.run = AsyncMock(return_value=mock_result)

        engine = GuardrailEngine()
        # First: flagged (ambiguous selector bypasses regex)
        result1 = await engine.check_action(
            "click", {"selector": "text=Proceed with action"}
        )
        assert not result1.allowed
        assert result1.needs_confirmation
        # Retry: confirmed
        result2 = await engine.check_action(
            "click", {"selector": "text=Proceed with action"}
        )
        assert result2.allowed

    @patch("guardrails.engine._destructive_checker")
    async def test_allows_safe_via_haiku(self, mock_agent):
        """Haiku returning destructive=false allows the action."""
        from guardrails.engine import DestructiveCheckResult

        mock_result = MagicMock()
        mock_result.output = DestructiveCheckResult(
            destructive=False, reason="navigation link"
        )
        mock_result.usage.return_value = MagicMock(input_tokens=50, output_tokens=20)
        mock_agent.run = AsyncMock(return_value=mock_result)

        engine = GuardrailEngine()
        # Use ambiguous selector that bypasses regex → reaches Haiku
        result = await engine.check_action(
            "click", {"selector": "text=Continue to step 2"}
        )
        assert result.allowed

    @patch("guardrails.engine._destructive_checker")
    async def test_caches_approved_selectors(self, mock_agent):
        """Second call with same selector uses cache, not API."""
        from guardrails.engine import DestructiveCheckResult

        mock_result = MagicMock()
        mock_result.output = DestructiveCheckResult(destructive=False, reason="safe")
        mock_result.usage.return_value = MagicMock(input_tokens=50, output_tokens=20)
        mock_agent.run = AsyncMock(return_value=mock_result)

        engine = GuardrailEngine()
        # Use ambiguous selector that bypasses regex → reaches Haiku
        await engine.check_action("click", {"selector": "text=Run analysis"})
        await engine.check_action("click", {"selector": "text=Run analysis"})
        assert mock_agent.run.call_count == 1

    @patch("guardrails.engine._destructive_checker")
    async def test_blocks_on_api_error_in_degraded_mode(self, mock_agent):
        """API errors block ambiguous actions in degraded mode."""
        mock_agent.run = AsyncMock(side_effect=Exception("API timeout"))

        engine = GuardrailEngine()
        # Use ambiguous selector that bypasses regex → reaches Haiku (which errors)
        result = await engine.check_action("click", {"selector": "text=Finalize batch"})
        assert not result.allowed
        assert "validation unavailable" in (result.reason or "")
