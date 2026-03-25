"""Tests for the profile loader."""

from __future__ import annotations

import pytest

from guardrails import GuardrailConfig
from profiles.loader import apply_guardrail_overrides, list_profiles, load_profile


class TestLoadProfile:
    def test_loads_default_profile(self):
        profile = load_profile("default")
        assert profile.name == "default"
        assert profile.prompt_extension is None

    def test_loads_research_profile(self):
        profile = load_profile("research")
        assert profile.name == "research"
        assert profile.prompt_extension is not None
        assert "Research" in profile.prompt_extension

    def test_loads_form_filling_profile(self):
        profile = load_profile("form_filling")
        assert profile.name == "form_filling"
        assert profile.prompt_extension is not None

    def test_missing_profile_raises(self):
        with pytest.raises(ValueError, match="not found"):
            load_profile("nonexistent_profile")


class TestListProfiles:
    def test_lists_built_in_profiles(self):
        profiles = list_profiles()
        assert "default" in profiles
        assert "research" in profiles
        assert "form_filling" in profiles


class TestApplyGuardrailOverrides:
    def test_no_overrides_returns_base(self):
        profile = load_profile("default")
        config = apply_guardrail_overrides(profile)
        assert config.max_urls_visited == 50  # default

    def test_research_overrides(self):
        profile = load_profile("research")
        config = apply_guardrail_overrides(profile)
        assert config.max_urls_visited == 100
        assert config.blocked_action_categories == []

    def test_form_filling_overrides(self):
        profile = load_profile("form_filling")
        config = apply_guardrail_overrides(profile)
        assert "account_modify" in config.blocked_action_categories
        assert "send_message" in config.blocked_action_categories
        # purchase and form_submit should NOT be blocked
        assert "purchase" not in config.blocked_action_categories
        assert "form_submit" not in config.blocked_action_categories

    def test_overrides_merge_with_base(self):
        profile = load_profile("research")
        base = GuardrailConfig(max_consecutive_errors=10)
        config = apply_guardrail_overrides(profile, base)
        # Profile override applied
        assert config.max_urls_visited == 100
        # Base value preserved where not overridden
        assert config.max_consecutive_errors == 10
