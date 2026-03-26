"""Tests for config — centralized CUA configuration."""

import pytest

from config import CUAConfig
from exceptions import ConfigError
from settings import AGENT_MODEL


class TestCUAConfigFromEnv:
    def test_basic_config_from_env(self, monkeypatch):
        monkeypatch.setenv("DIRECTIVE", "Go to example.com")
        monkeypatch.setenv("MODEL", AGENT_MODEL)
        monkeypatch.setenv("MAX_STEPS", "25")
        monkeypatch.setenv("PROFILE", "default")
        config = CUAConfig.from_env()
        assert config.directive == "Go to example.com"
        assert config.model == AGENT_MODEL
        assert config.max_steps == 25
        assert config.profile_name == "default"
        assert config.profile is not None

    def test_missing_directive_raises(self, monkeypatch):
        monkeypatch.delenv("DIRECTIVE", raising=False)
        with pytest.raises(ConfigError, match="DIRECTIVE"):
            CUAConfig.from_env()

    def test_credentials_from_env(self, monkeypatch):
        monkeypatch.setenv("DIRECTIVE", "test")
        monkeypatch.setenv(
            "CREDENTIALS_JSON", '{"github": {"username": "admin", "password": "pw"}}'
        )
        config = CUAConfig.from_env()
        assert config.credentials == {"github": {"username": "admin", "password": "pw"}}

    def test_guardrails_from_env(self, monkeypatch):
        monkeypatch.setenv("DIRECTIVE", "test")
        monkeypatch.setenv("GUARDRAILS_JSON", '{"max_urls_visited": 100}')
        config = CUAConfig.from_env()
        assert config.guardrail_config is not None
        assert config.guardrail_config.max_urls_visited == 100

    def test_defaults(self, monkeypatch):
        monkeypatch.setenv("DIRECTIVE", "test")
        # Clear optional vars
        for var in (
            "MODEL",
            "MAX_STEPS",
            "THINKING_BUDGET",
            "WIDTH",
            "HEIGHT",
            "START_URL",
            "PROXY_URL",
            "CREDENTIALS_JSON",
            "GUARDRAILS_JSON",
        ):
            monkeypatch.delenv(var, raising=False)
        config = CUAConfig.from_env()
        assert config.model == AGENT_MODEL
        assert config.max_steps == 50
        assert config.thinking_budget == 4096
        assert config.start_url is None
        assert config.proxy_url is None
