"""Tests for the profile loader."""

from __future__ import annotations

import pytest

from guardrails import GuardrailConfig
from profiles.loader import apply_guardrail_overrides, list_profiles, load_profile


class TestLoadProfile:
    def test_loads_default_profile(self):
        profile = load_profile("default")
        assert profile.name == "default"
        assert profile.prompt_extension is not None
        assert "Be precise" in profile.prompt_extension

    def test_missing_profile_raises(self):
        with pytest.raises(ValueError, match="not found"):
            load_profile("nonexistent_profile")


class TestListProfiles:
    def test_lists_built_in_profiles(self):
        profiles = list_profiles()
        assert "default" in profiles


class TestApplyGuardrailOverrides:
    def test_default_profile_keeps_safe_guardrails(self):
        profile = load_profile("default")
        config = apply_guardrail_overrides(profile)
        assert config.enable_llm_action_check is True
        assert config.allow_private_networks is False
        assert config.max_urls_visited == 50
        assert config.max_consecutive_errors == 5

    def test_overrides_merge_with_base(self):
        profile = load_profile("default")
        base = GuardrailConfig(max_urls_visited=100)
        config = apply_guardrail_overrides(profile, base)
        # Explicit base value takes precedence over profile override
        assert config.max_urls_visited == 100
        # No profile overrides exist, so base values are preserved.
        assert config.max_consecutive_errors == 5
